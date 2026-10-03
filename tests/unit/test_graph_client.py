import httpx
import pytest

from app.sharepoint.client import GraphApiError, GraphClient
from tests.conftest import FakeGraphAuth


def _client(settings, handler) -> GraphClient:
    transport = httpx.MockTransport(handler)
    return GraphClient(FakeGraphAuth(), settings, transport=transport)


@pytest.mark.asyncio
async def test_get_paged_follows_next_link_until_exhausted(settings):
    """
    Spec section 14: get_paged must keep following @odata.nextLink --
    using the server-provided URL verbatim -- until a page omits it.
    """
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if str(request.url).endswith("/drives/abc/root/children"):
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "1", "name": "a.md", "file": {}}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/_next_page_2",
                },
            )
        if str(request.url).endswith("/_next_page_2"):
            return httpx.Response(
                200,
                json={
                    "value": [{"id": "2", "name": "b.md", "file": {}}],
                    "@odata.nextLink": "https://graph.microsoft.com/v1.0/_next_page_3",
                },
            )
        if str(request.url).endswith("/_next_page_3"):
            return httpx.Response(200, json={"value": [{"id": "3", "name": "c.md", "file": {}}]})
        raise AssertionError(f"unexpected URL requested: {request.url}")

    client = _client(settings, handler)
    items = await client.get_paged("/drives/abc/root/children")
    await client.aclose()

    assert [i["id"] for i in items] == ["1", "2", "3"]
    assert call_count["n"] == 3


@pytest.mark.asyncio
async def test_get_paged_single_page_no_next_link(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [{"id": "1"}, {"id": "2"}]})

    client = _client(settings, handler)
    items = await client.get_paged("/drives/abc/root/children")
    await client.aclose()

    assert len(items) == 2


@pytest.mark.asyncio
async def test_get_json_retries_on_429_then_succeeds(settings):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "throttled"})
        return httpx.Response(200, json={"ok": True})

    client = _client(settings, handler)
    result = await client.get_json("/sites/fake")
    await client.aclose()

    assert result == {"ok": True}
    assert attempts["n"] == 3


@pytest.mark.asyncio
async def test_get_json_raises_after_exhausting_retries(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = _client(settings, handler)
    with pytest.raises(GraphApiError) as exc_info:
        await client.get_json("/sites/fake")
    await client.aclose()

    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_get_json_does_not_retry_on_404(settings):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(404, json={"error": "not found"})

    client = _client(settings, handler)
    with pytest.raises(GraphApiError):
        await client.get_json("/sites/fake")
    await client.aclose()

    assert attempts["n"] == 1


@pytest.mark.asyncio
async def test_get_bytes_returns_binary_content(settings):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x89PNGfake-bytes")

    client = _client(settings, handler)
    content = await client.get_bytes("/drives/abc/items/1/content")
    await client.aclose()

    assert content == b"\x89PNGfake-bytes"


@pytest.mark.asyncio
async def test_absolute_url_used_verbatim_not_reconstructed(settings):
    """
    A deltaLink/nextLink is an absolute Graph URL. The client must call it
    exactly as given, not re-prefix it with graph_base_url.
    """
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"value": []})

    client = _client(settings, handler)
    absolute = "https://graph.microsoft.com/v1.0/drives/abc/root/delta?token=xyz"
    await client.get_json(absolute)
    await client.aclose()

    assert seen_urls == [absolute]


@pytest.mark.asyncio
async def test_get_bytes_follows_content_redirect_and_drops_auth_header(settings):
    """
    Regression: Graph's /content endpoint replies 302 to a temporary
    download URL. The client must follow it (previously it returned the
    empty redirect body, so every file hashed as SHA-256 of nothing), and
    must not forward the bearer token to the download host.
    """
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("authorization")))
        if request.url.host == "graph.microsoft.com":
            return httpx.Response(302, headers={"Location": "https://download.example.com/tmp?tempauth=abc"})
        return httpx.Response(200, content=b"real file bytes")

    client = _client(settings, handler)
    content = await client.get_bytes("/drives/abc/items/1/content")
    await client.aclose()

    assert content == b"real file bytes"
    assert seen[0] == ("graph.microsoft.com", "Bearer fake-token")
    assert seen[1][0] == "download.example.com"
    assert seen[1][1] is None  # token not leaked to the redirect target
