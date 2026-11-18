"""
Full reconciliation (spec sections 23/53): a complete walk marks tracked
files SharePoint no longer has as deleted -- SharePoint wins (section 52).
"""
import httpx
import pytest

from app.db.models import ProcessingStatus, SourceType
from app.sharepoint.client import GraphClient
from app.sharepoint.drives import DiscoveredFile, DiscoveryResult
from app.sync.manager import run_initial_sync
from app.sync.state import DocumentUpsert, InMemoryDocumentRepository
from tests.conftest import FakeGraphAuth


def _discovered(item_id: str, name: str) -> DiscoveredFile:
    return DiscoveredFile(
        item_id=item_id, name=name, extension="." + name.rsplit(".", 1)[-1], size=5, web_url=f"https://x/{name}",
        parent_item_id=None, path=f"/{name}", etag=None, last_modified=None, is_supported=name.endswith(".md"),
    )


async def _track(repo: InMemoryDocumentRepository, item_id: str, name: str = "old.md") -> None:
    await repo.upsert(DocumentUpsert(
        source_type=SourceType.file, sharepoint_item_id=item_id, drive_id="drive1", site_id="site1",
        name=name, extension=".md", path=f"/{name}", sharepoint_url="u", parent_sharepoint_item_id=None,
        modified_at=None, etag=None, size=5, content_hash="old-hash", processing_status=ProcessingStatus.indexed,
    ))


def _client(settings, failing_ids: set[str] = frozenset()) -> GraphClient:
    def handler(request):
        if any(f"/items/{item_id}/content" in str(request.url) for item_id in failing_ids):
            return httpx.Response(404)
        return httpx.Response(200, content=b"current content")
    return GraphClient(FakeGraphAuth(), settings, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_walk_marks_files_missing_from_sharepoint_deleted(settings):
    repo = InMemoryDocumentRepository()
    await _track(repo, "still-there")
    await _track(repo, "gone")
    client = _client(settings)

    counters = await run_initial_sync(
        client, "drive1", "site1", repo, discovery=DiscoveryResult(files=[_discovered("still-there", "a.md")]),
    )
    await client.aclose()

    assert counters.items_deleted == 1
    assert (await repo.get_by_sharepoint_item_id(SourceType.file, "gone")).is_deleted is True
    assert (await repo.get_by_sharepoint_item_id(SourceType.file, "still-there")).is_deleted is False


@pytest.mark.asyncio
async def test_failed_download_is_not_mistaken_for_a_deletion(settings):
    repo = InMemoryDocumentRepository()
    await _track(repo, "flaky")
    client = _client(settings, failing_ids={"flaky"})

    counters = await run_initial_sync(
        client, "drive1", "site1", repo, discovery=DiscoveryResult(files=[_discovered("flaky", "a.md")]),
    )
    await client.aclose()

    assert counters.items_failed == 1
    assert counters.items_deleted == 0
    assert (await repo.get_by_sharepoint_item_id(SourceType.file, "flaky")).is_deleted is False


@pytest.mark.asyncio
async def test_file_renamed_to_unsupported_extension_is_marked_deleted(settings):
    repo = InMemoryDocumentRepository()
    await _track(repo, "renamed")
    client = _client(settings)

    counters = await run_initial_sync(
        client, "drive1", "site1", repo,
        discovery=DiscoveryResult(skipped_unsupported=[_discovered("renamed", "now.mp4")]),
    )
    await client.aclose()

    assert counters.items_deleted == 1
    assert (await repo.get_by_sharepoint_item_id(SourceType.file, "renamed")).is_deleted is True


@pytest.mark.asyncio
async def test_pages_are_never_touched_by_file_reconciliation(settings):
    repo = InMemoryDocumentRepository()
    await repo.upsert(DocumentUpsert(
        source_type=SourceType.page, sharepoint_item_id="page-1", drive_id=None, site_id="site1",
        name="Home", extension=".aspx", path=None, sharepoint_url="u", parent_sharepoint_item_id=None,
        modified_at=None, etag=None, size=5, content_hash="h",
    ))
    client = _client(settings)

    await run_initial_sync(client, "drive1", "site1", repo, discovery=DiscoveryResult())
    await client.aclose()

    assert (await repo.get_by_sharepoint_item_id(SourceType.page, "page-1")).is_deleted is False
