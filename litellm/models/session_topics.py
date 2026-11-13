from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from typing import Final, Literal, Protocol, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, SecretStr

TOPIC_MAX_CHARACTERS: Final = 50
TOPIC_PROMPT_VERSION: Final = "english-topic-v1"
TopicCategory: TypeAlias = Literal["engineering", "operations", "data_analysis", "writing", "general", "other", "unknown"]


class TopicState(str, Enum):
    PENDING = "pending"
    READY = "ready"
    INSUFFICIENT_INPUT = "insufficient_input"
    INVALID_OUTPUT = "invalid_output"
    UPSTREAM_FAILURE = "upstream_failure"
    CAPACITY = "capacity"
    EXPIRED = "expired"


class TopicConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    gateway_url: str = "http://127.0.0.1:4000/v1"
    model: str = "session-topic-luna"
    virtual_key: SecretStr = SecretStr("")
    identity_secret: SecretStr = SecretStr("")
    prior_worker_key_hashes: frozenset[str] = frozenset()
    allowed_key_hashes: frozenset[str] = frozenset()
    queue_capacity: int = Field(default=32, ge=1, le=256)
    concurrency: int = Field(default=2, ge=1, le=8)
    input_token_limit: int = Field(default=2000, ge=256, le=2000)
    output_token_limit: int = Field(default=256, ge=64, le=256)
    timeout_seconds: float = Field(default=10, ge=1, le=20)
    retention_days: int = Field(default=30, ge=1, le=90)
    memory_ttl_seconds: int = Field(default=60, ge=10, le=120)

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> TopicConfig:
        return cls(
            enabled=env.get("LITELLM_SESSION_TOPICS_ENABLED", "false").lower() == "true",
            gateway_url=env.get("LITELLM_SESSION_TOPICS_GATEWAY_URL", "http://127.0.0.1:4000/v1"),
            model=env.get("LITELLM_SESSION_TOPICS_MODEL", "session-topic-luna"),
            virtual_key=SecretStr(env.get("LITELLM_SESSION_TOPICS_VIRTUAL_KEY", "")),
            identity_secret=SecretStr(env.get("LITELLM_SESSION_TOPICS_IDENTITY_SECRET", "")),
            allowed_key_hashes=frozenset(filter(None, env.get("LITELLM_SESSION_TOPICS_ALLOWED_KEYS", "").split(","))),
            prior_worker_key_hashes=frozenset(
                filter(None, env.get("LITELLM_SESSION_TOPICS_PRIOR_WORKER_KEYS", "").split(","))
            ),
        )


class TopicScope(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    session_ref: str = Field(pattern=r"^[a-f0-9]{64}$")
    key_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    user_id: str | None = None
    team_id: str | None = None
    organization_id: str | None = None


class TopicResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    topic: str = Field(min_length=1, max_length=TOPIC_MAX_CHARACTERS)
    category: TopicCategory


class TopicAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    state: TopicState
    result: TopicResult | None = None


class TopicWorkerKey(BaseModel):
    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    models: tuple[str, ...]
    key_type: str | None
    max_budget: float | None
    rpm_limit: int | None
    tpm_limit: int | None


class TopicRecord(TopicScope):
    topic: str | None = None
    category: TopicCategory | None = None
    status: TopicState
    attempts: int
    model: str
    prompt_version: str
    first_seen: datetime
    last_seen: datetime
    updated_at: datetime


class TopicViewer(BaseModel):
    model_config = ConfigDict(frozen=True)

    actor_ref: str
    admin: bool = False
    user_id: str | None = None
    key_hash: str | None = None


class TopicPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: tuple[TopicRecord, ...]
    limit: int
    offset: int


class TopicUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: Literal["business", "session_topic", "other"]
    requests: int
    successful: int
    failed: int
    tokens: int
    spend: float


class TopicOverview(BaseModel):
    model_config = ConfigDict(frozen=True)

    sessions: int
    ready: int
    pending: int
    unavailable: int
    usage: tuple[TopicUsage, ...]


class TopicStore(Protocol):
    async def observe(self, scope: TopicScope, model: str, state: TopicState) -> None: ...

    async def claim(self, scope: TopicScope, model: str, lease: str) -> bool: ...

    async def finish(self, session_ref: str, lease: str, attempt: TopicAttempt) -> None: ...

    async def list_topics(self, viewer: TopicViewer, limit: int, offset: int) -> TopicPage: ...

    async def overview(self, viewer: TopicViewer, worker_hashes: tuple[str, ...], days: int) -> TopicOverview: ...

    async def audit_read(self, viewer: TopicViewer, operation: str, result_count: int) -> None: ...

    async def purge(self, days: int) -> None: ...


class TopicGenerator(Protocol):
    async def generate(self, text: str, repair: bool) -> TopicAttempt: ...
