from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, SecretStr, TypeAdapter, ValidationError

from litellm.models.session_topics import (
    TopicConfig,
    TopicCostAttribution,
    TopicCostKind,
    TopicCostLogMetadata,
    TopicCostStatus,
    TopicScope,
)

TOPIC_PARENT_CONTEXT: Final = "_litellm_session_topic_parent"
TOPIC_COST_CONTEXT: Final = "_litellm_session_topic_cost"
_MAPPING: Final = TypeAdapter(Mapping[str, object])


class CostContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    session_ref: str = Field(pattern=r"^[a-f0-9]{64}$")
    key_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    kind: TopicCostKind
    proof: str = Field(pattern=r"^[a-f0-9]{64}$")


class ParentContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    session_ref: str = Field(pattern=r"^[a-f0-9]{64}$")
    key_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires: int
    proof: str = Field(pattern=r"^[a-f0-9]{64}$")


def _mapping(value: object) -> Mapping[str, object]:
    try:
        return _MAPPING.validate_python(value)
    except ValidationError:
        return MappingProxyType({})


def _proof(secret: SecretStr, domain: str, values: tuple[str | int, ...]) -> str:
    material: Final = json.dumps((domain, *values), separators=(",", ":")).encode()
    return hmac.new(secret.get_secret_value().encode(), material, hashlib.sha256).hexdigest()


def parent_context(config: TopicConfig, scope: TopicScope, now: float) -> ParentContext:
    key_hash: Final = hashlib.sha256(config.virtual_key.get_secret_value().encode()).hexdigest()
    expires: Final = int(now) + 90
    return ParentContext(
        session_ref=scope.session_ref,
        key_hash=key_hash,
        expires=expires,
        proof=_proof(config.identity_secret, "topic-parent-v1", (scope.session_ref, key_hash, expires)),
    )


def verify_parent(raw: object, config: TopicConfig, key_hash: str, now: float) -> str | None:
    try:
        context: Final = ParentContext.model_validate(raw)
    except ValidationError:
        return None
    if context.key_hash != key_hash or not now < context.expires <= now + 90:
        return None
    expected: Final = _proof(
        config.identity_secret, "topic-parent-v1", (context.session_ref, key_hash, context.expires)
    )
    return context.session_ref if hmac.compare_digest(context.proof, expected) else None


def cost_context(config: TopicConfig, session_ref: str, key_hash: str, kind: TopicCostKind) -> CostContext:
    return CostContext(
        session_ref=session_ref,
        key_hash=key_hash,
        kind=kind,
        proof=_proof(config.identity_secret, "topic-cost-v1", (session_ref, key_hash, kind)),
    )


def verify_cost_context(raw: object, secret: SecretStr, key_hash: object) -> TopicCostAttribution | None:
    try:
        context: Final = CostContext.model_validate(raw)
    except ValidationError:
        return None
    if context.key_hash != key_hash:
        return None
    expected: Final = _proof(secret, "topic-cost-v1", (context.session_ref, context.key_hash, context.kind))
    if not hmac.compare_digest(context.proof, expected):
        return None
    return TopicCostAttribution(version=1, session_ref=context.session_ref, kind=context.kind)


def without_topic_proofs(metadata: object) -> dict[str, object] | None:  # mutable-ok: spend-log JSON uses dictionaries
    try:
        parsed: Final = _MAPPING.validate_python(metadata)
    except ValidationError:
        return None
    return {
        name: value
        for name, value in parsed.items()
        if name not in (TOPIC_PARENT_CONTEXT, TOPIC_COST_CONTEXT)
    }


def cost_status(payload: Mapping[str, object] | None) -> TopicCostStatus:
    if payload is None:
        return "unknown"
    diagnostic: Final = _mapping(payload.get("zero_cost_diagnostic"))
    if diagnostic.get("reason") == "missing_pricing_key":
        return "unpriced"
    pricing: Final = _mapping(payload.get("model_map_information")).get("model_map_value")
    if payload.get("response_cost_failure_debug_info") is not None:
        return "unpriced" if pricing is None else "unknown"
    if diagnostic:
        return "unknown"
    amount: Final = payload.get("response_cost")
    if isinstance(amount, bool) or not isinstance(amount, int | float) or not math.isfinite(amount):
        return "unknown"
    if amount != 0 or payload.get("cache_hit") is True:
        return "known"
    if payload.get("status") == "failure" and not payload.get("total_tokens"):
        return "unknown"
    if payload.get("cost_finalized") is True or isinstance(payload.get("cost_breakdown"), Mapping):
        return "known"
    return "unknown"


def topic_cost_log_metadata(
    metadata: object,
    key_hash: object,
    payload: Mapping[str, object] | None,
    *,
    environment: Mapping[str, str] | None = None,
) -> TopicCostLogMetadata | None:
    env: Final = os.environ if environment is None else environment
    if env.get("LITELLM_SESSION_TOPICS_ENABLED", "false").lower() != "true":
        return None
    secret: Final = SecretStr(env.get("LITELLM_SESSION_TOPICS_IDENTITY_SECRET", ""))
    if len(secret.get_secret_value()) < 32:
        return None
    spend: Final = _mapping(_mapping(metadata).get("spend_logs_metadata"))
    return TopicCostLogMetadata(
        session_topic=verify_cost_context(spend.get(TOPIC_COST_CONTEXT), secret, key_hash),
        session_topic_cost_status=cost_status(payload),
    )
