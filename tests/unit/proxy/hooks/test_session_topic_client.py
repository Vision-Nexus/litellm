import json
from typing import Final

import httpx
import pytest
from pydantic import SecretStr

from litellm.models.session_topics import TopicConfig, TopicState
from litellm.proxy.hooks.session_topic_client import LunaTopicGenerator


def response_body(topic: str) -> dict[str, object]:
    return {
        "id": "test-response",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"topic": topic, "category": "engineering"}),
                },
            }
        ],
    }


@pytest.mark.asyncio
async def test_summary_uses_virtual_key_structured_output_and_no_persistence_flags() -> None:
    captured: Final[list[httpx.Request]] = []  # mutable-ok: records calls at the external HTTP boundary

    def transport(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=response_body("Investigating database connection timeouts"))

    config: Final = TopicConfig(virtual_key=SecretStr("test-virtual-key"), gateway_url="http://gateway.test/v1")
    generator: Final = LunaTopicGenerator(config, httpx.AsyncClient(transport=httpx.MockTransport(transport)))
    try:
        result: Final = await generator.generate("Investigate a database timeout", repair=False)
    finally:
        await generator.aclose()
    assert result.state == TopicState.READY
    assert len(captured) == 1
    request: Final = captured[0]
    body: Final = json.loads(request.content)
    assert request.headers["authorization"] == "Bearer test-virtual-key"
    assert str(request.url) == "http://gateway.test/v1/chat/completions"
    assert body["model"] == config.model
    assert body["reasoning_effort"] == "none"
    assert body["store"] is False
    assert body["stream"] is False
    assert body["cache"] == {"no-cache": True, "no-store": True}
    assert "no-store" in request.headers["cache-control"]
    assert body["max_completion_tokens"] == config.output_token_limit
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["properties"]["topic"]["maxLength"] == 50
    assert "tools" not in body


@pytest.mark.asyncio
async def test_oversized_topic_is_rejected_without_truncation() -> None:
    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body("Investigating intermittent database timeout errors."))

    generator: Final = LunaTopicGenerator(
        TopicConfig(virtual_key=SecretStr("test-key")),
        httpx.AsyncClient(
            transport=httpx.MockTransport(transport),
        ),
    )
    try:
        result: Final = await generator.generate("Investigate database errors", repair=False)
    finally:
        await generator.aclose()
    assert result.state == TopicState.INVALID_OUTPUT
    assert result.result is None


@pytest.mark.asyncio
async def test_provider_error_does_not_retry_or_return_the_error_body(caplog: pytest.LogCaptureFixture) -> None:
    calls: Final[list[str]] = []  # mutable-ok: records external requests

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(429, json={"error": {"message": "private-error-canary", "type": "rate_limit"}})

    generator: Final = LunaTopicGenerator(
        TopicConfig(virtual_key=SecretStr("test-key")),
        httpx.AsyncClient(
            transport=httpx.MockTransport(transport),
        ),
    )
    try:
        result: Final = await generator.generate("Investigate database errors", repair=False)
    finally:
        await generator.aclose()
    assert calls == ["POST"]
    assert result.state == TopicState.UPSTREAM_FAILURE
    assert "private-error-canary" not in result.model_dump_json()
    assert "private-error-canary" not in caplog.text
