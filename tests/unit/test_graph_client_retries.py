"""Transient transport failures (spec section 43) and backoff caps (section 44)."""
import httpx
import pytest

from app.sharepoint.client import GraphApiError, GraphClient
from tests.conftest import FakeGraphAuth


def _client(settings, handler) -> GraphClient:
    return GraphClient(FakeGraphAuth(), settings, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_connection_error_is_retried_then_succeeds(settings):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection reset", request=request)
        if calls["n"] == 2:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"ok": True})

    client = _client(settings, handler)
    assert await client.get_json("/sites/x") == {"ok": True}
    await client.aclose()
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_exhausted_transport_retries_raise_graph_api_error_without_url(settings):
    secret_url = "https://download.example.com/file?tempauth=SECRET"

    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    client = _client(settings, handler)
    with pytest.raises(GraphApiError) as exc_info:
        await client.get_bytes(secret_url)
    await client.aclose()

    assert exc_info.value.status_code is None
    assert "SECRET" not in str(exc_info.value)
    assert "ConnectError" in str(exc_info.value)


@pytest.mark.asyncio
async def test_retry_after_is_capped(settings, monkeypatch):
    settings.http_max_backoff_seconds = 0.05
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("app.sharepoint.client.asyncio.sleep", fake_sleep)
    responses = iter([httpx.Response(429, headers={"Retry-After": "3600"}), httpx.Response(200, json={})])

    client = _client(settings, lambda request: next(responses))
    await client.get_json("/sites/x")
    await client.aclose()

    assert sleeps == [0.05]


@pytest.mark.asyncio
async def test_extra_headers_survive_retries(settings):
    seen: list[str | None] = []
    responses = iter([httpx.Response(503), httpx.Response(200, json={})])

    def handler(request):
        seen.append(request.headers.get("X-Test"))
        return next(responses)

    client = _client(settings, handler)
    await client._request_with_retry("GET", "https://graph.microsoft.com/v1.0/x", headers={"X-Test": "1"})
    await client.aclose()

    assert seen == ["1", "1"]
