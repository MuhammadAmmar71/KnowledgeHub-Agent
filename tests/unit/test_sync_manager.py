import hashlib

import httpx
import pytest

from app.db.models import ProcessingStatus, SourceType
from app.sharepoint.client import GraphClient
from app.sharepoint.drives import DiscoveredFile
from app.sync.manager import ProcessingOutcome, process_discovered_file, run_initial_sync
from app.sync.state import InMemoryDocumentRepository
from tests.conftest import FakeGraphAuth


def _client(settings, handler) -> GraphClient:
    return GraphClient(FakeGraphAuth(), settings, transport=httpx.MockTransport(handler))


def _discovered(item_id="item-1", name="test.md") -> DiscoveredFile:
    return DiscoveredFile(
        item_id=item_id, name=name, extension=".md", size=42,
        web_url=f"https://example.sharepoint.com/{name}", parent_item_id=None,
        path=f"/{name}", etag="\"v1\"", last_modified="2026-01-01T00:00:00Z",
        is_supported=True,
    )


@pytest.mark.asyncio
async def test_new_file_is_added(settings):
    def handler(request):
        return httpx.Response(200, content=b"hello world")

    repo = InMemoryDocumentRepository()
    client = _client(settings, handler)
    outcome = await process_discovered_file(client, "drive1", "site1", _discovered(), repo)
    await client.aclose()

    assert outcome == ProcessingOutcome.added
    doc = await repo.get_by_sharepoint_item_id(SourceType.file, "item-1")
    assert doc is not None
    assert doc.content_hash == hashlib.sha256(b"hello world").hexdigest()
    assert doc.processing_status == ProcessingStatus.pending


@pytest.mark.asyncio
async def test_rerun_with_unchanged_content_does_not_reprocess(settings):
    """
    Spec section 28/42 (idempotency): same item + same content hash =
    no re-embedding / no reprocessing, only a metadata touch.
    """
    def handler(request):
        return httpx.Response(200, content=b"same content every time")

    repo = InMemoryDocumentRepository()
    client = _client(settings, handler)

    first = await process_discovered_file(client, "drive1", "site1", _discovered(), repo)
    second = await process_discovered_file(client, "drive1", "site1", _discovered(), repo)
    await client.aclose()

    assert first == ProcessingOutcome.added
    assert second == ProcessingOutcome.unchanged


@pytest.mark.asyncio
async def test_modified_content_triggers_reprocess_with_new_hash(settings):
    """Spec section 20 Modified: same item ID, different hash -> old chunks replaced (future phase), new hash stored now."""
    responses = iter([b"version one", b"version two -- changed"])

    def handler(request):
        return httpx.Response(200, content=next(responses))

    repo = InMemoryDocumentRepository()
    client = _client(settings, handler)

    first = await process_discovered_file(client, "drive1", "site1", _discovered(), repo)
    second = await process_discovered_file(client, "drive1", "site1", _discovered(), repo)
    await client.aclose()

    assert first == ProcessingOutcome.added
    assert second == ProcessingOutcome.modified

    doc = await repo.get_by_sharepoint_item_id(SourceType.file, "item-1")
    assert doc.content_hash == hashlib.sha256(b"version two -- changed").hexdigest()


@pytest.mark.asyncio
async def test_download_failure_is_recorded_not_raised(settings):
    """Spec section 43: permanent/transient errors are recorded, not left to crash the whole run."""

    def handler(request):
        return httpx.Response(404)

    repo = InMemoryDocumentRepository()
    client = _client(settings, handler)
    outcome = await process_discovered_file(client, "drive1", "site1", _discovered(), repo)
    await client.aclose()

    assert outcome == ProcessingOutcome.failed
    doc = await repo.get_by_sharepoint_item_id(SourceType.file, "item-1")
    assert doc.processing_status == ProcessingStatus.download_failed
    assert doc.retry_count == 1


@pytest.mark.asyncio
async def test_run_initial_sync_counts_added_modified_skipped_failed(settings):
    """
    End-to-end (within the mocked-Graph boundary) check of the counters
    that a sync_run row will eventually store.
    """
    call_log = []

    def handler(request):
        url = str(request.url)
        call_log.append(url)
        if url.endswith("/drives/drive1/root/children"):
            return httpx.Response(200, json={"value": [
                {"id": "good-1", "name": "good.md", "file": {}, "size": 5, "webUrl": "u1"},
                {"id": "bad-1", "name": "bad.md", "file": {}, "size": 5, "webUrl": "u2"},
                {"id": "skip-1", "name": "movie.mp4", "file": {}, "size": 5, "webUrl": "u3"},
            ]})
        if "/items/good-1/content" in url:
            return httpx.Response(200, content=b"good content")
        if "/items/bad-1/content" in url:
            return httpx.Response(503)
        raise AssertionError(f"unexpected url {url}")

    repo = InMemoryDocumentRepository()
    client = _client(settings, handler)
    counters = await run_initial_sync(client, "drive1", "site1", repo)
    await client.aclose()

    assert counters.items_detected == 3  # 2 supported + 1 unsupported
    assert counters.items_added == 1
    assert counters.items_failed == 1
    assert counters.items_skipped == 1
    assert counters.has_failures is True
