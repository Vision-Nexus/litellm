from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Final

import pytest
import yaml
from pydantic import JsonValue

from tests.integration._support.budget import readiness_seconds
from tests.integration._support.client import (
    JSON_OBJECT,
    Gateway,
    eventually,
    gateway_from_environment,
    held,
    list_value,
    object_value,
    string_value,
)
from tests.integration._support.process import OwnedProxy, graceful_stop_seconds, owned_proxy_process, private_state
from tests.integration._support.wire import Reply, Request, Wire, wire_server

pytestmark: Final = pytest.mark.timeout(2 * readiness_seconds(2) + 2 * graceful_stop_seconds() + 180)
MODEL: Final = "session-topic-luna"
TOPIC: Final = "Reviewing database connection errors"
SECRET: Final = "sk-synthetic-topic-canary-1234567890"
TEXT: Final = f"Please investigate database connection errors. api_key={SECRET}"


def _frame(body: Mapping[str, JsonValue]) -> bytes:
    return b"data: " + json.dumps(body).encode() + b"\n\n"


def _upstream(request: Request) -> Reply:
    if request.method == "GET" and request.target == "/v1/models":
        return Reply(body=b'{"object":"list","data":[]}')
    assert request.target.endswith("/chat/completions"), request.target
    body: Final = JSON_OBJECT.validate_json(request.body)
    summary: Final = object_value(body.get("response_format", {})).get("type") == "json_schema"
    content: Final = json.dumps({"topic": TOPIC, "category": "engineering"}) if summary else "Synthetic reply"
    if summary:
        messages: Final = list_value(body["messages"])
        assert len(messages) == 2, messages
        candidate: Final = string_value(object_value(messages[-1])["content"])
        assert "database connection errors" in candidate, candidate
        assert SECRET not in candidate and "PRIVATE_HISTORY" not in candidate and "PRIVATE_SYSTEM" not in candidate
        assert body["store"] is False and body["reasoning_effort"] == "none", body
        assert body["max_completion_tokens"] == 256, body
    identity: Final = "chatcmpl-" + uuid.uuid4().hex
    usage: Final[dict[str, JsonValue]] = {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}
    common: Final[dict[str, JsonValue]] = {"id": identity, "created": 1, "model": "gpt-6-luna"}
    if body.get("stream") is True:
        chunk: Final = {**common, "object": "chat.completion.chunk"}
        return Reply(
            content_type="text/event-stream",
            chunks=(
                _frame({**chunk, "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}}]}),
                _frame({**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}),
                b"data: [DONE]\n\n",
            ),
        )
    return Reply(
        body=json.dumps(
            {
                **common,
                "object": "chat.completion",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": usage,
            }
        ).encode()
    )


class SummaryRelay:
    def __init__(self) -> None:
        self.gateway: Gateway | None = None

    def __call__(self, request: Request) -> Reply:
        gateway: Final = self.gateway
        assert gateway is not None, "Summary arrived before proxy readiness"
        body: Final = JSON_OBJECT.validate_json(request.body)
        response: Final = gateway.request(
            "POST",
            request.target,
            {**body, "litellm_session_id": "internal-summary-session"},
            headers={
                "Authorization": request.headers["authorization"],
                "Cache-Control": request.headers["cache-control"],
                "x-litellm-enable-message-redaction": "true",
            },
        )
        return Reply(status=response.status_code, body=response.content)


def _configuration(directory: Path, upstream: Wire) -> Path:
    config: Final = directory / "topics.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "model_list": [
                    {
                        "model_name": MODEL,
                        "litellm_params": {
                            "model": "openai/gpt-6-luna",
                            "api_base": f"{upstream.url}/v1",
                            "api_key": "synthetic-provider-key",
                            "use_chat_completions_api": True,
                            "input_cost_per_token": 0.001,
                            "output_cost_per_token": 0.002,
                        },
                    }
                ],
                "general_settings": {
                    "master_key": "os.environ/LITELLM_MASTER_KEY",
                    "database_url": "os.environ/DATABASE_URL",
                    "disable_model_info_refresh": True,
                    "store_prompts_in_spend_logs": False,
                    "disable_spend_logs": False,
                    "proxy_batch_write_at": 1,
                    "proxy_batch_polling_interval": 1,
                },
                "litellm_settings": {
                    "turn_off_message_logging": True,
                    "set_verbose": False,
                    "request_correlation_in_logs": True,
                },
                "router_settings": {
                    "num_retries": 0,
                    "redis_host": "os.environ/REDIS_HOST",
                    "redis_port": "os.environ/REDIS_PORT",
                },
            }
        )
    )
    return config


@dataclass(frozen=True, slots=True)
class TopicGateway:
    gateway: Gateway
    process: OwnedProxy
    worker_key: str


@pytest.fixture(scope="module")
def topic_gateway(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TopicGateway]:
    directory: Final = tmp_path_factory.mktemp("session-topics-gateway")
    relay: Final = SummaryRelay()
    with ExitStack() as stack:
        upstream: Final = stack.enter_context(wire_server(_upstream))
        ingress: Final = stack.enter_context(wire_server(relay, keep_alive=True))
        shared: Final = stack.enter_context(gateway_from_environment())
        state: Final = stack.enter_context(private_state(directory, os.environ))
        config: Final = _configuration(directory, upstream)
        bootstrap: Final = stack.enter_context(
            owned_proxy_process(
                shared,
                directory,
                {**state.environment, "LITELLM_SESSION_TOPICS_ENABLED": "false"},
                config=config,
            )
        )
        seed: Final = replace(bootstrap.gateway, database_url=state.database_url)
        scenario: Final = stack.enter_context(seed.scenario())
        worker_key: Final = scenario.key(
            models=[MODEL], key_type="llm_api", max_budget=10, rpm_limit=100, tpm_limit=100000
        )
        proxy: Final = stack.enter_context(
            owned_proxy_process(
                shared,
                directory,
                {
                    **state.environment,
                    "LITELLM_LOG_LEVEL": "WARNING",
                    "LITELLM_SESSION_TOPICS_ENABLED": "true",
                    "LITELLM_SESSION_TOPICS_GATEWAY_URL": f"{ingress.url}/v1",
                    "LITELLM_SESSION_TOPICS_MODEL": MODEL,
                    "LITELLM_SESSION_TOPICS_VIRTUAL_KEY": worker_key,
                    "LITELLM_SESSION_TOPICS_IDENTITY_SECRET": "synthetic-session-identity-secret-for-integration",
                    "LITELLM_SESSION_TOPICS_ALLOWED_KEYS": "*",
                },
                config=config,
                workers=2,
            )
        )
        gateway: Final = replace(proxy.gateway, database_url=state.database_url)
        relay.gateway = gateway
        yield TopicGateway(gateway, proxy, worker_key)


def _send(gateway: Gateway, key: str, endpoint: str, session: str, stream: bool = False) -> None:
    messages: Final[list[JsonValue]] = [
        {"role": "user", "content": "PRIVATE_HISTORY"},
        {"role": "assistant", "content": "Previous synthetic reply"},
        {"role": "user", "content": TEXT},
    ]
    payload: Final[dict[str, JsonValue]] = (
        {"input": messages, "instructions": "PRIVATE_SYSTEM"}
        if endpoint == "responses"
        else {
            "messages": messages,
            **({"system": "PRIVATE_SYSTEM", "max_tokens": 64} if endpoint == "messages" else {}),
        }
    )
    response: Final = gateway.request(
        "POST",
        f"/v1/{endpoint}",
        {"model": MODEL, "litellm_session_id": session, "stream": stream, **payload},
        key=key,
        headers={"connection": "close"},
    )
    assert response.status_code == 200, response.text
    assert "Synthetic reply" in response.text, response.text


def _topics(gateway: Gateway, key: str) -> tuple[dict[str, JsonValue], ...]:
    response: Final = gateway.request("GET", "/session_topics", key=key)
    assert response.status_code == 200, response.text
    return tuple(object_value(item) for item in list_value(JSON_OBJECT.validate_json(response.content)["items"]))


@pytest.mark.parametrize("endpoint", ("chat/completions", "responses", "messages"))
@pytest.mark.parametrize("stream", (False, True))
def test_live_proxy_records_one_private_topic_per_session(
    topic_gateway: TopicGateway, endpoint: str, stream: bool
) -> None:
    gateway: Final = topic_gateway.gateway
    with gateway.scenario() as scenario:
        key: Final = scenario.key(models=[MODEL])
        session: Final = "topic-" + uuid.uuid4().hex
        _send(gateway, key, endpoint, session, stream)
        records: Final = eventually(
            lambda: _topics(gateway, key),
            lambda rows: len(rows) == 1 and rows[0]["status"] == "ready",
            seconds=60,
        )
        assert records[0]["topic"] == TOPIC and records[0]["attempts"] == 1, records
        assert records[0]["key_hash"] == sha256(key.encode()).hexdigest(), records
        _send(gateway, key, endpoint, session, stream)
        unchanged: Final = held(
            lambda: _topics(gateway, key),
            lambda rows: len(rows) == 1 and rows[0]["attempts"] == 1,
            holding=1,
        )
        assert unchanged[0]["session_ref"] == records[0]["session_ref"], unchanged
        persisted: Final = gateway.rows(
            'SELECT to_jsonb(t)::text AS stored FROM "LiteLLM_SessionTopic" t WHERE key_hash=%s',
            (sha256(key.encode()).hexdigest(),),
        )
        assert session not in json.dumps(persisted) and SECRET not in json.dumps(persisted), persisted
        assert SECRET not in topic_gateway.process.log.read_text(), "Raw input appeared in proxy logs"


def test_live_auth_isolates_sibling_keys_and_separates_summary_spend(topic_gateway: TopicGateway) -> None:
    gateway: Final = topic_gateway.gateway
    with gateway.scenario() as scenario:
        user: Final = scenario.user()
        first: Final = scenario.key(models=[MODEL], user_id=user)
        sibling: Final = scenario.key(models=[MODEL], user_id=user)
        session: Final = "topic-" + uuid.uuid4().hex
        _send(gateway, first, "chat/completions", session)
        _send(gateway, sibling, "chat/completions", session)
        first_rows: Final = eventually(
            lambda: _topics(gateway, first),
            lambda rows: len(rows) == 1 and rows[0]["status"] == "ready",
            seconds=60,
        )
        sibling_rows: Final = eventually(
            lambda: _topics(gateway, sibling),
            lambda rows: len(rows) == 1 and rows[0]["status"] == "ready",
            seconds=60,
        )
        assert first_rows[0]["session_ref"] != sibling_rows[0]["session_ref"]
        denied: Final = gateway.request("GET", "/session_topics", key=topic_gateway.worker_key)
        assert denied.status_code == 403, denied.text
        worker_hash: Final = sha256(topic_gateway.worker_key.encode()).hexdigest()
        assert gateway.rows('SELECT session_ref FROM "LiteLLM_SessionTopic" WHERE key_hash=%s', (worker_hash,)) == []
        ready: Final = gateway.rows("SELECT count(*) AS count FROM \"LiteLLM_SessionTopic\" WHERE status='ready'", ())
        summary_rows: Final = eventually(
            lambda: gateway.rows(
                'SELECT count(*) AS requests, sum(spend)::float8 AS spend FROM "LiteLLM_SpendLogs" WHERE api_key=%s',
                (worker_hash,),
            ),
            lambda rows: rows[0]["requests"] == ready[0]["count"],
            seconds=70,
        )
        overview: Final = gateway.get("/session_topics/overview")
        categories: Final = tuple(object_value(item) for item in list_value(overview["usage"]))
        internal: Final = next(item for item in categories if item["kind"] == "session_topic")
        assert internal["requests"] == summary_rows[0]["requests"], overview
        assert internal["spend"] == pytest.approx(summary_rows[0]["spend"]), overview
        own: Final = eventually(
            lambda: gateway.request("GET", "/session_topics/overview", key=first),
            lambda response: response.status_code == 200 and bool(JSON_OBJECT.validate_json(response.content)["usage"]),
            seconds=70,
        )
        assert own.status_code == 200, own.text
        own_usage: Final = list_value(JSON_OBJECT.validate_json(own.content)["usage"])
        assert len(own_usage) == 1 and object_value(own_usage[0])["kind"] == "business", own.text
        assert object_value(own_usage[0])["requests"] == 1, own.text
