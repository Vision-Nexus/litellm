from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping, Sequence
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType
from typing import Final, Protocol, cast  # noqa: TID251  # langdetect has no typed factory interface.

from pydantic import TypeAdapter, ValidationError

from litellm.constants import SESSION_ID_GENERATED_METADATA_KEY
from litellm.litellm_core_utils.llm_judge import extract_text_from_content
from litellm.models.session_topics import TopicResult, TopicScope

_SESSION: Final = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_HASH: Final = re.compile(r"^[a-f0-9]{64}$")
_SECRET: Final = re.compile(
    r"(?i)\b(?:sk-|ghp_|github_pat_|xox[baprs]-|AKIA)[A-Za-z0-9_-]+"
    r"|\bBearer\s+\S+|\b(?:password|passwd|api[_ -]?key|secret|access[_ -]?token)\s*[:=]\s*\S+"
    r"|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?"
)
_IDENTIFIER: Final = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    r"|https?://\S+|\b(?:\d{1,3}\.){3}\d{1,3}\b|\b\d[\d ()+-]{7,}\d\b"
    r"|\b[A-Za-z0-9_-]{24,}\b|(?:[A-Za-z]:\\|/home/|/Users/|/etc/)\S+"
)
_CODE: Final = re.compile(r"```[\s\S]*?(?:```|$)|`[^`]*`")
_PRINTABLE_TOPIC: Final = re.compile(r"[A-Za-z][A-Za-z0-9 ,.'()+/#&:-]{0,49}")
_OBJECT_MAPPING: Final = TypeAdapter(Mapping[str, object])
_OBJECT_SEQUENCE: Final = TypeAdapter(Sequence[object])
_EMPTY: Final[Mapping[str, object]] = MappingProxyType({})


def object_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        try:
            return _OBJECT_MAPPING.validate_python(value)
        except ValidationError:
            return _EMPTY
    return _EMPTY


def hash_key(value: str) -> str:
    return value if _HASH.fullmatch(value) else hashlib.sha256(value.encode()).hexdigest()


def session_id(data: Mapping[str, object]) -> str | None:
    primary: Final = object_mapping(data.get("litellm_metadata"))
    metadata: Final = object_mapping(data.get("metadata"))
    headers: Final = object_mapping(primary.get("headers") or metadata.get("headers"))
    if primary.get(SESSION_ID_GENERATED_METADATA_KEY) or metadata.get(SESSION_ID_GENERATED_METADATA_KEY):
        return None
    candidates: Final = (
        data.get("litellm_session_id"),
        primary.get("session_id"),
        metadata.get("session_id"),
        headers.get("x-litellm-session-id"),
        headers.get("session-id"),
        headers.get("session_id"),
        headers.get("thread-id"),
        headers.get("conversation_id"),
        headers.get("x-opencode-session"),
    )
    return next(
        (
            value
            for value in candidates
            if isinstance(value, str) and _SESSION.fullmatch(value) and not _SECRET.search(value)
        ),
        None,
    )


def make_scope(
    secret: str, key: str, session: str, user_id: str | None, team_id: str | None, organization_id: str | None
) -> TopicScope:
    key_hash: Final = hash_key(key)
    identity: Final = json.dumps(("v1", organization_id, team_id, key_hash, session), separators=(",", ":"))
    return TopicScope(
        session_ref=hmac.new(secret.encode(), identity.encode(), hashlib.sha256).hexdigest(),
        key_hash=key_hash,
        user_id=user_id,
        team_id=team_id,
        organization_id=organization_id,
    )


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content[:8192]
    if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
        sequence: Final = _OBJECT_SEQUENCE.validate_python(content)
        parts: Final = tuple(object_mapping(part) for part in sequence[:16])
        normalized: Final = [
            {"type": "text", "text": str(part["text"])[:8192]}
            for part in parts
            if part.get("type") in ("text", "input_text") and isinstance(part.get("text"), str)
        ]
        return extract_text_from_content(normalized)[:8192]
    return ""


def latest_user_text(data: Mapping[str, object]) -> str:
    messages: Final = data.get("messages", data.get("input"))
    if isinstance(messages, str):
        return sanitize_input(messages[:8192])
    if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)):
        return ""
    sequence: Final = _OBJECT_SEQUENCE.validate_python(messages)
    users: Final = tuple(object_mapping(message) for message in sequence[-128:])
    last: Final = next((message for message in reversed(users) if message.get("role") == "user"), _EMPTY)
    return sanitize_input(_content_text(last.get("content")))


def sanitize_input(text: str) -> str:
    without_code: Final = _CODE.sub(" ", text)
    without_secrets: Final = _SECRET.sub("[redacted]", without_code)
    without_identifiers: Final = _IDENTIFIER.sub("[redacted]", without_secrets)
    return " ".join(without_identifiers.split())[:8192]


class _LanguageDetector(Protocol):
    def append(self, text: str) -> None: ...

    def detect(self) -> str: ...


class _LanguageFactory(Protocol):
    def load_profile(self, directory: str) -> None: ...

    def set_seed(self, seed: int) -> None: ...

    def create(self) -> _LanguageDetector: ...


@lru_cache(maxsize=1)
def _language_factory() -> _LanguageFactory:
    from langdetect.detector_factory import DetectorFactory  # pyright: ignore[reportMissingTypeStubs]  # Methods are narrowed through _LanguageFactory.

    factory: Final = cast(  # cast-ok: typed boundary for the untyped language library
        _LanguageFactory, DetectorFactory()
    )
    factory.load_profile(str(files("langdetect").joinpath("profiles")))
    factory.set_seed(0)
    return factory


def validate_topic(raw: str) -> TopicResult | None:
    from langdetect.lang_detect_exception import LangDetectException  # pyright: ignore[reportMissingTypeStubs]  # Upstream does not ship exception stubs.

    try:
        result: Final = TopicResult.model_validate_json(raw)
    except ValidationError:
        return None
    if result.topic != result.topic.strip() or not _PRINTABLE_TOPIC.fullmatch(result.topic):
        return None
    if _SECRET.search(result.topic) or _IDENTIFIER.search(result.topic):
        return None
    detector: Final = _language_factory().create()
    try:
        detector.append(result.topic)
        if detector.detect() != "en":
            return None
    except LangDetectException:
        return None
    return result
