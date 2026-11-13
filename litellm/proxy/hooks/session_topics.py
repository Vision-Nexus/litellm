from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Final, cast  # noqa: TID251  # Prisma raw-query methods lack async typings.
from urllib.parse import urlsplit

from fastapi import FastAPI
from pydantic import TypeAdapter

import litellm
from litellm.integrations.custom_logger import CustomLogger
from litellm.litellm_core_utils.session_topic_text import (
    hash_key,
    latest_user_text,
    make_scope,
    session_id,
    validate_topic,
)
from litellm.models.session_topics import TopicConfig
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.hooks.session_topic_client import LunaTopicGenerator
from litellm.proxy.hooks.session_topic_engine import TopicEngine
from litellm.repositories.session_topics_repository import SessionTopicsRepository, TopicDatabase

if TYPE_CHECKING:
    from litellm.caching.caching import DualCache
    from litellm.proxy.utils import PrismaClient

_LOG: Final = logging.getLogger("litellm.session_topics")
_CALL_TYPES: Final = frozenset(("completion", "acompletion", "responses", "aresponses", "anthropic_messages"))
_OPTIONAL_STRING: Final[TypeAdapter[str | None]] = TypeAdapter(str | None)


def authenticated_key(auth: UserAPIKeyAuth) -> str | None:
    value: Final = auth.token or auth.api_key
    return hash_key(value) if value else None


class SessionTopicHook(CustomLogger):
    def __init__(self, config: TopicConfig, engine: TopicEngine) -> None:
        super().__init__(turn_off_message_logging=True)  # pyright: ignore[reportUnknownMemberType]  # Legacy extension constructor has untyped kwargs.
        self.config = config
        self.engine = engine
        self.worker_hashes = (
            frozenset((hash_key(config.virtual_key.get_secret_value()),)) | config.prior_worker_key_hashes
        )

    async def async_pre_call_hook(
        self, user_api_key_dict: UserAPIKeyAuth, cache: DualCache, data: Mapping[str, object], call_type: str
    ) -> dict[str, object] | None:  # mutable-ok: LiteLLM's hook contract requires a request dictionary
        key: Final = authenticated_key(user_api_key_dict)
        if key is None:
            return None
        if key in self.worker_hashes:
            return {
                **data,
                "store": False,
                "cache": {"no-cache": True, "no-store": True},
                "num_retries": 0,
                "disable_fallbacks": True,
            }
        if call_type not in _CALL_TYPES or not self.config.enabled:
            return None
        if "*" not in self.config.allowed_key_hashes and key not in self.config.allowed_key_hashes:
            return None
        session: Final = session_id(data)
        if session is None:
            self.engine.unassociated_requests += 1
            return None
        scope: Final = make_scope(
            self.config.identity_secret.get_secret_value(),
            key,
            session,
            user_api_key_dict.user_id,
            user_api_key_dict.team_id,
            _OPTIONAL_STRING.validate_python(getattr(user_api_key_dict, "organization_id", None)),
        )
        self.engine.submit(scope, latest_user_text(data))
        return None


class SessionTopicRuntime:
    def __init__(self, config: TopicConfig, store: SessionTopicsRepository, generator: LunaTopicGenerator) -> None:
        self.config = config
        self.store = store
        self.generator = generator
        self.engine = TopicEngine(config, store, generator)
        self.hook = SessionTopicHook(config, self.engine)
        self.cleanup: asyncio.Task[None] | None = None
        self.closed = False

    def start(self) -> None:
        self.engine.start()
        litellm.logging_callback_manager.add_litellm_callback(self.hook)
        self.cleanup = asyncio.create_task(self._purge())

    async def _purge(self) -> None:
        while True:
            try:
                await self.store.purge(self.config.retention_days)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001  # Do not serialize database errors into application logs.
                _LOG.warning("session_topic_retention_failed")
            await asyncio.sleep(3600)

    async def stop(self) -> None:
        if self.closed:
            return
        self.closed = True
        await self.engine.stop()
        if self.cleanup is not None:
            self.cleanup.cancel()
            await asyncio.gather(self.cleanup, return_exceptions=True)
        litellm.logging_callback_manager.remove_callback_from_list_by_object(litellm.callbacks, self.hook)  # pyright: ignore[reportUnknownMemberType]  # Legacy callback registry has untyped parameters.
        await self.generator.aclose()


async def validate_configuration(
    config: TopicConfig, store: SessionTopicsRepository, general_settings: Mapping[str, object]
) -> None:
    if validate_topic('{"topic":"Reviewing database connection errors","category":"engineering"}') is None:
        raise ValueError("Session topic English validation is unavailable")
    url: Final = urlsplit(config.gateway_url)
    if (
        url.scheme not in ("http", "https")
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
    ):
        raise ValueError("Session topics require an explicit HTTP(S) LiteLLM gateway URL without credentials")
    if len(config.identity_secret.get_secret_value()) < 32:
        raise ValueError("Session topic identity secret must contain at least 32 characters")
    if not config.virtual_key.get_secret_value():
        raise ValueError("Session topics require a dedicated virtual key")
    if (
        not litellm.turn_off_message_logging
        or litellm.set_verbose
        or os.environ.get("LITELLM_LOG_LEVEL", "").upper() == "DEBUG"
    ):
        raise ValueError("Session topics require message redaction and disabled DEBUG logging")
    if general_settings.get("store_prompts_in_spend_logs", False) is not False:
        raise ValueError("Session topics require prompt storage in spend logs to be disabled")
    row: Final = await store.worker_key(hashlib.sha256(config.virtual_key.get_secret_value().encode()).hexdigest())
    if row is None:
        raise ValueError("Session topic credential must be a registered virtual key, not the master key")
    if row.models != (config.model,) or row.key_type != "llm_api":
        raise ValueError("Session topic key must allow only its model and use the llm_api key type")
    if any(value is None or value <= 0 for value in (row.max_budget, row.rpm_limit, row.tpm_limit)):
        raise ValueError("Session topic key requires explicit budget, RPM and TPM limits")
    await store.verify_schema()


@asynccontextmanager
async def session_topics_lifespan(
    app: FastAPI | None, prisma_client: PrismaClient | None, general_settings: Mapping[str, object]
) -> AsyncGenerator[None, None]:
    config: Final = TopicConfig.from_environment(os.environ)
    if not config.enabled:
        yield
        return
    if app is None:
        raise ValueError("Session topics require an ASGI application")
    if prisma_client is None:
        raise ValueError("Session topics require a configured database")
    store: Final = SessionTopicsRepository(
        lambda: cast(TopicDatabase, prisma_client.writer_db)  # cast-ok: borrow the current writer across reconnects
    )
    await validate_configuration(config, store, general_settings)
    await store.register_worker(hash_key(config.virtual_key.get_secret_value()))
    for prior in config.prior_worker_key_hashes:
        await store.register_worker(prior)
    runtime: Final = SessionTopicRuntime(config, store, LunaTopicGenerator(config))
    app.state.session_topics = runtime  # rebind-ok: publish the lifespan-owned service for request dependencies
    try:
        runtime.start()
        yield
    finally:
        await runtime.stop()
        app.state.session_topics = None  # rebind-ok: remove the closed lifespan-owned service


async def stop_session_topics(app: FastAPI | None) -> None:
    state: Final[object] = getattr(app, "state", None)
    runtime: Final = getattr(state, "session_topics", None)
    if isinstance(runtime, SessionTopicRuntime):
        await runtime.stop()
