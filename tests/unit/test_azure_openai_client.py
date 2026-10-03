import json

import httpx
import pytest

from app.llm.azure_openai import AzureOpenAIResponsesClient, normalize_endpoint
from app.llm.base import LLMError

ENDPOINT = "https://example-llm.openai.azure.com/openai/v1"


def _ok_body(text="Hello [1]."):
    return {
        "model": "gpt-4o-2024-11-20",
        "status": "completed",
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 120, "output_tokens": 8},
    }


def _client(handler, **kwargs):
    return AzureOpenAIResponsesClient(
        ENDPOINT, "secret-key", "gpt-4o", backoff_base_seconds=0.0,
        transport=httpx.MockTransport(handler), **kwargs,
    )


def test_normalize_endpoint_accepts_full_responses_uri():
    assert normalize_endpoint(ENDPOINT + "/responses") == ENDPOINT
    assert normalize_endpoint(ENDPOINT + "/") == ENDPOINT
    assert normalize_endpoint(ENDPOINT) == ENDPOINT


@pytest.mark.asyncio
async def test_request_shape_and_parsed_response():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["api_key"] = request.headers.get("api-key")
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_ok_body())

    client = _client(handler, temperature=0.2, max_output_tokens=500)
    response = await client.generate("be careful", "Question: hi")
    await client.aclose()

    assert seen["url"] == ENDPOINT + "/responses"
    assert seen["api_key"] == "secret-key"
    assert seen["auth"] is None
    assert seen["body"] == {
        "model": "gpt-4o", "instructions": "be careful", "input": "Question: hi",
        "temperature": 0.2, "max_output_tokens": 500,
    }
    assert response.text == "Hello [1]."
    assert (response.input_tokens, response.output_tokens) == (120, 8)
    assert response.incomplete_reason is None


@pytest.mark.asyncio
async def test_incomplete_response_reports_reason():
    body = _ok_body("Partial")
    body["status"] = "incomplete"
    body["incomplete_details"] = {"reason": "max_output_tokens"}
    client = _client(lambda r: httpx.Response(200, json=body))
    response = await client.generate("i", "u")
    await client.aclose()
    assert response.text == "Partial"
    assert response.incomplete_reason == "max_output_tokens"


@pytest.mark.asyncio
async def test_retries_on_429_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json=_ok_body("ok"))

    client = _client(handler)
    response = await client.generate("i", "u")
    await client.aclose()
    assert len(calls) == 3
    assert response.text == "ok"


@pytest.mark.asyncio
async def test_non_retryable_error_raises_with_status_and_never_leaks_key():
    def handler(request):
        return httpx.Response(404, json={"error": {"code": "DeploymentNotFound", "message": "no such deployment"}})

    client = _client(handler)
    with pytest.raises(LLMError) as info:
        await client.generate("i", "u")
    await client.aclose()
    assert info.value.status_code == 404
    assert "DeploymentNotFound" in str(info.value)
    assert "secret-key" not in str(info.value)


@pytest.mark.asyncio
async def test_retries_exhausted_raises():
    client = _client(lambda r: httpx.Response(503, text="busy"), max_retries=2)
    with pytest.raises(LLMError) as info:
        await client.generate("i", "u")
    await client.aclose()
    assert info.value.status_code == 503


def test_missing_key_fails_fast():
    with pytest.raises(LLMError, match="AZURE_OPENAI_API_KEY"):
        AzureOpenAIResponsesClient(ENDPOINT, "", "gpt-4o")
