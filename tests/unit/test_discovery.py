import httpx
import pytest

from app.sharepoint.client import GraphClient
from app.sharepoint.drives import walk_drive
from tests.conftest import FakeGraphAuth


def _client(settings, handler) -> GraphClient:
    transport = httpx.MockTransport(handler)
    return GraphClient(FakeGraphAuth(), settings, transport=transport)


@pytest.mark.asyncio
async def test_nested_folder_discovery_finds_deeply_buried_file(settings):
    """
    Mirrors acceptance Test 4 in the spec: A/B/C/test.md must be discovered
    via iterative (not Python-recursion-based) traversal.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/drives/drive1/root/children"):
            return httpx.Response(200, json={"value": [
                {"id": "folder-a", "name": "A", "folder": {}},
            ]})
        if url.endswith("/drives/drive1/items/folder-a/children"):
            return httpx.Response(200, json={"value": [
                {"id": "folder-b", "name": "B", "folder": {}},
            ]})
        if url.endswith("/drives/drive1/items/folder-b/children"):
            return httpx.Response(200, json={"value": [
                {"id": "folder-c", "name": "C", "folder": {}},
            ]})
        if url.endswith("/drives/drive1/items/folder-c/children"):
            return httpx.Response(200, json={"value": [
                {
                    "id": "file-test",
                    "name": "test.md",
                    "file": {},
                    "size": 123,
                    "webUrl": "https://example.sharepoint.com/test.md",
                    "eTag": "\"abc\"",
                    "lastModifiedDateTime": "2026-01-01T00:00:00Z",
                },
            ]})
        raise AssertionError(f"unexpected url {url}")

    client = _client(settings, handler)
    result = await walk_drive(client, "drive1")
    await client.aclose()

    assert len(result.files) == 1
    assert result.files[0].name == "test.md"
    assert result.files[0].path == "/A/B/C/test.md"
    assert result.files[0].is_supported is True
    assert result.folders_visited == 4  # root + A + B + C


@pytest.mark.asyncio
async def test_unsupported_file_is_skipped_not_failed(settings):
    """Mirrors acceptance Test 5: video.mp4 is skipped, not an error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [
            {"id": "f1", "name": "test.md", "file": {}},
            {"id": "f2", "name": "video.mp4", "file": {}},
            {"id": "f3", "name": "Agent_Instructions.txt", "file": {}},
            {"id": "f4", "name": "02-code-states.svg", "file": {}},
        ]})

    client = _client(settings, handler)
    result = await walk_drive(client, "drive1")
    await client.aclose()

    supported_names = {f.name for f in result.files}
    skipped_names = {f.name for f in result.skipped_unsupported}
    assert supported_names == {"test.md", "Agent_Instructions.txt", "02-code-states.svg"}
    assert skipped_names == {"video.mp4"}


@pytest.mark.asyncio
async def test_discovery_follows_pagination_within_a_folder(settings):
    """Mirrors acceptance Test 6: multiple Graph pages, all items discovered."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/drives/drive1/root/children"):
            return httpx.Response(200, json={
                "value": [{"id": "f1", "name": "one.md", "file": {}}],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/_page2",
            })
        if url.endswith("/_page2"):
            return httpx.Response(200, json={"value": [{"id": "f2", "name": "two.md", "file": {}}]})
        raise AssertionError(f"unexpected url {url}")

    client = _client(settings, handler)
    result = await walk_drive(client, "drive1")
    await client.aclose()

    assert {f.name for f in result.files} == {"one.md", "two.md"}


@pytest.mark.asyncio
async def test_folder_content_is_never_downloaded(settings):
    """
    A folder item must never trigger a /content call -- walk_drive only
    enumerates metadata; it doesn't call get_bytes at all.
    """
    requested_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if "children" in str(request.url):
            return httpx.Response(200, json={"value": [
                {"id": "folder-a", "name": "A", "folder": {}},
            ]}) if "folder-a" not in str(request.url) else httpx.Response(200, json={"value": []})
        raise AssertionError(f"unexpected url {request.url}")

    client = _client(settings, handler)
    await walk_drive(client, "drive1")
    await client.aclose()

    assert all("/content" not in u for u in requested_urls)


@pytest.mark.asyncio
async def test_package_facet_without_file_or_folder_is_ignored(settings):
    """An item with neither 'file' nor 'folder' facet is skipped, not crashed on."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": [
            {"id": "weird", "name": "onenote.one", "package": {"type": "oneNote"}},
        ]})

    client = _client(settings, handler)
    result = await walk_drive(client, "drive1")
    await client.aclose()

    assert result.files == []
    assert result.skipped_unsupported == []
