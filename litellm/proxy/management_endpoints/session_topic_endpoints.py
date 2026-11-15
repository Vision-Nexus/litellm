from __future__ import annotations

import hashlib
import hmac
from typing import Annotated, Final, TypeAlias

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from litellm.constants import UI_SESSION_TOKEN_TEAM_ID
from litellm.models.session_topics import TopicOverview, TopicPage, TopicViewer
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth.user_api_key_auth import user_api_key_auth
from litellm.proxy.deps.repositories import SessionTopicsRepositoryDep
from litellm.proxy.hooks.session_topics import SessionTopicRuntime, authenticated_key

router: Final = APIRouter(tags=["Session topics"])
_Auth: TypeAlias = Annotated[UserAPIKeyAuth, Depends(user_api_key_auth)]


def topic_runtime(request: Request) -> SessionTopicRuntime:
    application: Final[object] = request.app  # pyright: ignore[reportAny]  # Starlette exposes an untyped app.
    state: Final[object] = getattr(application, "state", None)
    runtime: Final = getattr(state, "session_topics", None)
    if not isinstance(runtime, SessionTopicRuntime) or runtime.closed:
        raise HTTPException(status_code=503, detail="Session topics are disabled")
    return runtime


def topic_viewer(auth: UserAPIKeyAuth, runtime: SessionTopicRuntime) -> TopicViewer:
    key: Final = authenticated_key(auth)
    if key in runtime.hook.worker_hashes:
        raise HTTPException(status_code=403, detail="The summary service cannot read session topics")
    is_admin: Final = str(auth.user_role) in ("proxy_admin", "proxy_admin_viewer")
    owner: Final = auth.user_id if auth.team_id == UI_SESSION_TOKEN_TEAM_ID else None
    if not key and not owner and not is_admin:
        raise HTTPException(status_code=403, detail="A user or key identity is required")
    actor_identity: Final = f"actor-user:{owner}" if owner else f"actor-key:{key or auth.user_id}"
    actor: Final = hmac.new(
        runtime.config.identity_secret.get_secret_value().encode(),
        actor_identity.encode(),
        hashlib.sha256,
    ).hexdigest()
    return TopicViewer(
        actor_ref=actor,
        admin=is_admin,
        user_id=owner,
        key_hash=key,
    )


@router.get("/session_topics", response_model=TopicPage)
async def list_session_topics(
    request: Request,
    auth: _Auth,
    topics: SessionTopicsRepositoryDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100000)] = 0,
) -> TopicPage:
    runtime: Final = topic_runtime(request)
    viewer: Final = topic_viewer(auth, runtime)
    if topics is None:
        raise HTTPException(status_code=503, detail="Session topic database is unavailable")
    page: Final = await topics.list_topics(viewer, limit, offset)
    await topics.audit_read(viewer, "list", len(page.items))
    return page


@router.get("/session_topics/overview", response_model=TopicOverview)
async def session_topic_overview(
    request: Request, auth: _Auth, topics: SessionTopicsRepositoryDep, days: Annotated[int, Query(ge=1, le=30)] = 7
) -> TopicOverview:
    runtime: Final = topic_runtime(request)
    viewer: Final = topic_viewer(auth, runtime)
    if topics is None:
        raise HTTPException(status_code=503, detail="Session topic database is unavailable")
    result: Final = await topics.overview(viewer, tuple(runtime.hook.worker_hashes), days)
    await topics.audit_read(viewer, "overview", result.sessions)
    return result
