from typing import Final

import pytest
from pydantic import SecretStr

from litellm.litellm_core_utils.session_topic_cost import (
    TOPIC_COST_CONTEXT,
    TOPIC_PARENT_CONTEXT,
    cost_context,
    cost_status,
    parent_context,
    topic_cost_log_metadata,
    verify_cost_context,
    verify_parent,
    without_topic_proofs,
)
from litellm.litellm_core_utils.session_topic_text import hash_key, make_scope
from litellm.models.session_topics import TopicConfig

_SECRET: Final = SecretStr("synthetic-identity-secret-for-cost-tests")
_CONFIG: Final = TopicConfig(identity_secret=_SECRET, virtual_key=SecretStr("synthetic-worker-key"))


def test_summary_parent_context_is_bound_to_the_worker_and_expires() -> None:
    scope: Final = make_scope(_SECRET.get_secret_value(), "business", "session-one", None, None, None)
    parent: Final = parent_context(_CONFIG, scope, now=100.5)
    worker: Final = hash_key("synthetic-worker-key")
    assert verify_parent(parent.model_dump(), _CONFIG, worker, 101) == scope.session_ref
    assert verify_parent(parent.model_dump(), _CONFIG, hash_key("other-worker"), 101) is None
    assert verify_parent(parent.model_dump(), _CONFIG, worker, 190) is None
    tampered: Final = parent.model_copy(update={"session_ref": "f" * 64})
    assert verify_parent(tampered.model_dump(), _CONFIG, worker, 101) is None


def test_spend_context_cannot_be_moved_between_keys_or_changed_by_a_client() -> None:
    key: Final = hash_key("business")
    context: Final = cost_context(_CONFIG, "a" * 64, key, "business")
    assert verify_cost_context(context.model_dump(), _SECRET, key) == {
        "version": 1,
        "session_ref": "a" * 64,
        "kind": "business",
    }
    assert verify_cost_context(context.model_dump(), _SECRET, hash_key("sibling")) is None
    assert verify_cost_context({**context.model_dump(), "kind": "session_topic"}, _SECRET, key) is None
    assert verify_cost_context({**context.model_dump(), "session_ref": "b" * 64}, _SECRET, key) is None
    assert verify_cost_context({"session_ref": "a" * 64, "proof": "bad"}, _SECRET, key) is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        (None, "unknown"),
        ({"response_cost": 0.1}, "known"),
        ({"response_cost": -0.1}, "known"),
        ({"response_cost": float("nan")}, "unknown"),
        ({"response_cost": 0, "cache_hit": True}, "known"),
        ({"response_cost": 0, "cost_finalized": True, "status": "success"}, "known"),
        ({"response_cost": 0, "cost_breakdown": {}}, "unknown"),
        ({"response_cost": 0, "cost_breakdown": {"total_cost": 0}}, "unknown"),
        ({"response_cost": 0, "status": "failure", "total_tokens": 0}, "unknown"),
        ({"response_cost": 0, "zero_cost_diagnostic": {"reason": "missing_pricing_key"}}, "unpriced"),
        ({"response_cost": 0, "zero_cost_diagnostic": {"reason": "pricing_not_applied"}}, "unknown"),
        ({"response_cost": 0, "response_cost_failure_debug_info": {"error_str": "model not found"}}, "unpriced"),
        ({"response_cost": 0}, "unknown"),
    ),
)
def test_price_observation_does_not_confuse_missing_rates_with_free_calls(
    payload: dict[str, object] | None, expected: str
) -> None:
    assert cost_status(payload) == expected


def test_log_projection_keeps_only_verified_reference_and_canonical_price_status() -> None:
    key: Final = hash_key("business")
    context: Final = cost_context(_CONFIG, "a" * 64, key, "business")
    metadata: Final = {
        "session_topic_cost_status": "known",
        "spend_logs_metadata": {TOPIC_COST_CONTEXT: context.model_dump(), "customer_tag": "one"},
    }
    environment: Final = {
        "LITELLM_SESSION_TOPICS_ENABLED": "true",
        "LITELLM_SESSION_TOPICS_IDENTITY_SECRET": _SECRET.get_secret_value(),
    }
    result: Final = topic_cost_log_metadata(metadata, key, None, environment=environment)
    assert result == {
        "session_topic": {"version": 1, "session_ref": "a" * 64, "kind": "business"},
        "session_topic_cost_status": "unknown",
    }
    assert topic_cost_log_metadata(metadata, key, None, environment={}) is None
    assert without_topic_proofs({TOPIC_COST_CONTEXT: context, TOPIC_PARENT_CONTEXT: "private", "tag": "ok"}) == {
        "tag": "ok"
    }
