import hashlib

import httpx
import pytest

from app.db.models import SourceType
from app.sharepoint.client import GraphClient
from app.sync.manager import establish_delta_baseline, run_delta_sync
from app.sync.state import InMemoryDocumentRepository, InMemorySyncStateRepository
from tests.conftest import FakeGraphAuth


def _client(settings, handler) -> GraphClient:
    return GraphClient(FakeGraphAuth(), settings, transport=httpx.MockTransport(handler))


def _file_item(item_id="item-1", name="test.md", content=b"hello"):
    return {
        "id": item_id, "name": name, "file": {}, "size": len(content),
        "webUrl": f"https://example.sharepoint.com/{name}",
        "eTag": "\"v1\"", "lastModifiedDateTime": "2026-01-01T00:00:00Z",
        "parentReference": {"id": "parent-1"},
    }


@pytest.mark.asyncio
async def test_new_file_via_delta_is_added_and_cursor_advances(settings):
    def handler(request):
        url = str(request.url)
        if url.endswith("/drives/drive1/root/delta"):
            return httpx.Response(200, json={
                "value": [_file_item()],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/_delta_cursor_1",
            })
        if "/items/item-1/content" in url:
            return httpx.Response(200, content=b"hello world")
        raise AssertionError(f"unexpected url {url}")

    doc_repo = InMemoryDocumentRepository()
    state_repo = InMemorySyncStateRepository()
    client = _client(settings, handler)

    counters = await run_delta_sync(client, "drive1", "site1", doc_repo, state_repo)
    await client.aclose()

    assert counters.items_added == 1
    assert counters.items_failed == 0
    assert await state_repo.get_delta_link("document_library") == "https://graph.microsoft.com/v1.0/_delta_cursor_1"

    doc = await doc_repo.get_by_sharepoint_item_id(SourceType.file, "item-1")
    assert doc.content_hash == hashlib.sha256(b"hello world").hexdigest()


@pytest.mark.asyncio
async def test_deleted_facet_marks_document_deleted(settings):
    """Spec section 20 Deleted: delta item with a 'deleted' facet -> mark_deleted, chunks (future) deactivated."""

    def handler(request):
        url = str(request.url)
        if url.endswith("/drives/drive1/root/delta"):
            return httpx.Response(200, json={
                "value": [{"id": "item-1", "deleted": {"state": "deleted"}}],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/_delta_cursor_2",
            })
        raise AssertionError(f"unexpected url {url}")

    doc_repo = InMemoryDocumentRepository()
    state_repo = InMemorySyncStateRepository()
    # Pre-seed the document as if a prior sync had already indexed it.
    from app.sync.state import DocumentUpsert
    from app.db.models import ProcessingStatus
    await doc_repo.upsert(DocumentUpsert(
        source_type=SourceType.file, sharepoint_item_id="item-1", drive_id="drive1", site_id="site1",
        name="test.md", extension=".md", path="/test.md", sharepoint_url="u", parent_sharepoint_item_id=None,
        modified_at=None, etag=None, size=5, content_hash="abc", processing_status=ProcessingStatus.pending,
    ))

    client = _client(settings, handler)
    counters = await run_delta_sync(client, "drive1", "site1", doc_repo, state_repo)
    await client.aclose()

    assert counters.items_deleted == 1
    doc = await doc_repo.get_by_sharepoint_item_id(SourceType.file, "item-1")
    assert doc.is_deleted is True


@pytest.mark.asyncio
async def test_deleted_facet_for_untracked_item_is_skipped_not_failed(settings):
    """A delete for something we never indexed (e.g. it was unsupported) shouldn't count as a failure."""

    def handler(request):
        return httpx.Response(200, json={
            "value": [{"id": "never-seen", "deleted": {"state": "deleted"}}],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/_delta_cursor_3",
        })

    doc_repo = InMemoryDocumentRepository()
    state_repo = InMemorySyncStateRepository()
    client = _client(settings, handler)
    counters = await run_delta_sync(client, "drive1", "site1", doc_repo, state_repo)
    await client.aclose()

    assert counters.items_deleted == 0
    assert counters.items_skipped == 1
    assert counters.items_failed == 0


@pytest.mark.asyncio
async def test_folder_and_unsupported_items_are_skipped(settings):
    def handler(request):
        return httpx.Response(200, json={
            "value": [
                {"id": "folder-1", "name": "Some Folder", "folder": {}},
                {"id": "vid-1", "name": "clip.mp4", "file": {}, "size": 1, "webUrl": "u"},
            ],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/_delta_cursor_4",
        })

    doc_repo = InMemoryDocumentRepository()
    state_repo = InMemorySyncStateRepository()
    client = _client(settings, handler)
    counters = await run_delta_sync(client, "drive1", "site1", doc_repo, state_repo)
    await client.aclose()

    assert counters.items_skipped == 2
    assert counters.items_added == 0


@pytest.mark.asyncio
async def test_delta_pagination_follows_next_link_to_delta_link(settings):
    def handler(request):
        url = str(request.url)
        if url.endswith("/drives/drive1/root/delta"):
            return httpx.Response(200, json={
                "value": [_file_item("item-1", "one.md")],
                "@odata.nextLink": "https://graph.microsoft.com/v1.0/_delta_page2",
            })
        if url.endswith("/_delta_page2"):
            return httpx.Response(200, json={
                "value": [_file_item("item-2", "two.md")],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/_delta_final_cursor",
            })
        if "/content" in url:
            return httpx.Response(200, content=b"some content")
        raise AssertionError(f"unexpected url {url}")

    doc_repo = InMemoryDocumentRepository()
    state_repo = InMemorySyncStateRepository()
    client = _client(settings, handler)
    counters = await run_delta_sync(client, "drive1", "site1", doc_repo, state_repo)
    await client.aclose()

    assert counters.items_added == 2
    assert await state_repo.get_delta_link("document_library") == "https://graph.microsoft.com/v1.0/_delta_final_cursor"


@pytest.mark.asyncio
async def test_cursor_not_advanced_when_an_item_fails(settings):
    """
    Mirrors acceptance Test 7: a processing failure must leave the OLD
    cursor in place so the next run refetches this same batch and retries.
    """

    def handler(request):
        url = str(request.url)
        if url.endswith("/drives/drive1/root/delta"):
            return httpx.Response(200, json={
                "value": [_file_item("good-1", "good.md"), _file_item("bad-1", "bad.md")],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/_delta_should_not_be_saved",
            })
        if "/items/good-1/content" in url:
            return httpx.Response(200, content=b"ok content")
        if "/items/bad-1/content" in url:
            return httpx.Response(503)
        raise AssertionError(f"unexpected url {url}")

    doc_repo = InMemoryDocumentRepository()
    state_repo = InMemorySyncStateRepository()
    client = _client(settings, handler)
    counters = await run_delta_sync(client, "drive1", "site1", doc_repo, state_repo)
    await client.aclose()

    assert counters.items_added == 1
    assert counters.items_failed == 1
    assert counters.has_failures is True
    # Critical: the cursor must NOT have advanced.
    assert await state_repo.get_delta_link("document_library") is None


@pytest.mark.asyncio
async def test_retry_after_fix_advances_cursor(settings):
    """
    Mirrors acceptance Test 8: once the failing item succeeds on retry
    (same batch, since the cursor didn't move), the sync succeeds and the
    cursor finally advances. Re-processing the already-good item is
    harmless because it comes back "unchanged".
    """

    def handler(request):
        url = str(request.url)
        if url.endswith("/drives/drive1/root/delta"):
            return httpx.Response(200, json={
                "value": [_file_item("good-1", "good.md"), _file_item("bad-1", "bad.md")],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/_delta_cursor_recovered",
            })
        if "/content" in url:  # both items now download fine
            return httpx.Response(200, content=b"ok content")
        raise AssertionError(f"unexpected url {url}")

    doc_repo = InMemoryDocumentRepository()
    state_repo = InMemorySyncStateRepository()
    client = _client(settings, handler)
    counters = await run_delta_sync(client, "drive1", "site1", doc_repo, state_repo)
    await client.aclose()

    assert counters.items_failed == 0
    assert await state_repo.get_delta_link("document_library") == "https://graph.microsoft.com/v1.0/_delta_cursor_recovered"


@pytest.mark.asyncio
async def test_delta_fetch_failure_leaves_cursor_untouched(settings):
    """A network/Graph failure while paging must not advance or corrupt the cursor."""

    def handler(request):
        return httpx.Response(503)

    doc_repo = InMemoryDocumentRepository()
    state_repo = InMemorySyncStateRepository()
    client = _client(settings, handler)
    counters = await run_delta_sync(client, "drive1", "site1", doc_repo, state_repo)
    await client.aclose()

    assert counters.items_failed == 1
    assert await state_repo.get_delta_link("document_library") is None


@pytest.mark.asyncio
async def test_second_run_uses_saved_cursor_not_initial_endpoint(settings):
    state_repo = InMemorySyncStateRepository()
    await state_repo.record_success("document_library", "https://graph.microsoft.com/v1.0/_saved_cursor")

    seen_urls = []

    def handler(request):
        seen_urls.append(str(request.url))
        return httpx.Response(200, json={"value": [], "@odata.deltaLink": "https://graph.microsoft.com/v1.0/_new_cursor"})

    doc_repo = InMemoryDocumentRepository()
    client = _client(settings, handler)
    await run_delta_sync(client, "drive1", "site1", doc_repo, state_repo)
    await client.aclose()

    assert seen_urls == ["https://graph.microsoft.com/v1.0/_saved_cursor"]


@pytest.mark.asyncio
async def test_establish_delta_baseline_captures_cursor_without_reprocessing(settings):
    """
    After the initial full walk already handled every file, the baseline
    call should only page through for the cursor -- it must not trigger any
    /content download calls.
    """
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, json={
            "value": [_file_item("item-1", "already-indexed.md")],
            "@odata.deltaLink": "https://graph.microsoft.com/v1.0/_baseline_cursor",
        })

    state_repo = InMemorySyncStateRepository()
    client = _client(settings, handler)
    link = await establish_delta_baseline(client, "drive1", state_repo)
    await client.aclose()

    assert link == "https://graph.microsoft.com/v1.0/_baseline_cursor"
    assert await state_repo.get_delta_link("document_library") == "https://graph.microsoft.com/v1.0/_baseline_cursor"
    assert all("/content" not in u for u in requested)
