import json
from typing import Final

import pytest

from litellm.litellm_core_utils.session_topic_text import latest_user_text, make_scope, session_id, validate_topic


def test_fifty_character_english_topic_is_preserved() -> None:
    topic: Final = "Investigating intermittent database timeout errors"
    assert len(topic) == 50
    result: Final = validate_topic(json.dumps({"topic": topic, "category": "engineering"}))
    assert result is not None
    assert result.topic == topic


@pytest.mark.parametrize(
    "topic",
    (
        "Investigating intermittent database timeout errors.",
        "数据库连接超时排查",
        "Investigando errores de conexion",
        "Investigating 数据库 timeouts",
        "Email alice@example.com about database errors",
        "Debug sk-not-a-real-key-12345678901234567890",
        " Review database settings",
        "Review database settings ",
        "Review database settings\n",
    ),
)
def test_unsafe_non_english_or_oversized_topic_is_not_accepted(topic: str) -> None:
    assert validate_topic(json.dumps({"topic": topic, "category": "engineering"})) is None


def test_unrequested_output_fields_are_rejected() -> None:
    assert (
        validate_topic(
            json.dumps({"topic": "Investigating database timeouts", "category": "engineering", "input": "x"})
        )
        is None
    )


def test_latest_user_text_excludes_history_tools_code_and_identifiers() -> None:
    body: Final = {
        "messages": [
            {"role": "system", "content": "private-system-canary"},
            {"role": "user", "content": "old-history-canary"},
            {"role": "assistant", "content": "private-answer-canary"},
            {
                "role": "user",
                "content": "Fix database timeout alice@example.com ```secret source code``` api_key=secret-value",
            },
            {"role": "tool", "content": "private-tool-canary"},
        ]
    }
    assert latest_user_text(body) == "Fix database timeout [redacted] [redacted]"


def test_responses_text_blocks_are_supported_without_images_or_tool_results() -> None:
    body: Final = {
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Investigate database timeouts"},
                    {"type": "input_image", "image_url": "private-image-canary"},
                    {"type": "tool_result", "text": "private-tool-canary"},
                ],
            }
        ]
    }
    assert latest_user_text(body) == "Investigate database timeouts"


@pytest.mark.parametrize("field", ("metadata", "litellm_metadata"))
def test_explicit_session_is_preserved_but_generated_trace_is_not_a_session(field: str) -> None:
    assert session_id({field: {"session_id": "conversation-123"}}) == "conversation-123"
    assert session_id({field: {"session_id": "conversation-123", "litellm_session_id_generated": True}}) is None
    assert session_id({field: {"trace_id": "trace-only-123"}}) is None


def test_session_reference_is_stable_and_isolated_across_keys_and_tenants() -> None:
    secret: Final = "identity-secret-for-test-only-000000000000"
    first: Final = make_scope(secret, "test-key-a", "session-123", "user", "team", "org")
    assert first == make_scope(secret, "test-key-a", "session-123", "user", "team", "org")
    assert first.session_ref != make_scope(secret, "test-key-b", "session-123", "user", "team", "org").session_ref
    assert first.session_ref != make_scope(secret, "test-key-a", "session-123", "user", "team", "other").session_ref
    assert "session-123" not in first.model_dump_json()
    assert "test-key-a" not in first.model_dump_json()
