import httpx
import pytest

from app.chunking.chunker import Chunker
from app.db.models import ProcessingStatus, SourceType
from app.sharepoint.client import GraphClient
from app.sharepoint.pages import check_page_capability, normalize_page_content
from app.sync.manager import process_page, run_page_sync
from app.sync.state import InMemoryChunkRepository, InMemoryDocumentRepository
from tests.conftest import FakeGraphAuth


def _client(settings, handler) -> GraphClient:
    return GraphClient(FakeGraphAuth(), settings, transport=httpx.MockTransport(handler))


def _page_summary(page_id="page-1", title="My Page"):
    return {"id": page_id, "title": title, "webUrl": f"https://example.sharepoint.com/{page_id}", "lastModifiedDateTime": "2026-01-01T00:00:00Z"}


def _page_detail(page_id="page-1", title="My Page", html="<p>Hello <b>world</b></p>"):
    return {
        "id": page_id, "title": title, "description": "A test page", "webUrl": f"https://example.sharepoint.com/{page_id}",
        "canvasLayout": {"horizontalSections": [{"columns": [{"webparts": [{"innerHtml": html}]}]}]},
    }


# --- capability test ---------------------------------------------------

@pytest.mark.asyncio
async def test_capability_succeeds_when_graph_returns_200(settings):
    def handler(request):
        return httpx.Response(200, json={"value": []})

    client = _client(settings, handler)
    result = await check_page_capability(client, "site1")
    await client.aclose()

    assert result.available is True
    assert result.http_status == 200


@pytest.mark.asyncio
async def test_capability_reports_permission_denied_distinctly(settings):
    def handler(request):
        return httpx.Response(403, json={"error": {"code": "Forbidden"}})

    client = _client(settings, handler)
    result = await check_page_capability(client, "site1")
    await client.aclose()

    assert result.available is False
    assert result.http_status == 403
    assert "Sites.Read.All" in result.detail  # names the real permission gap, not a generic message


@pytest.mark.asyncio
async def test_capability_distinguishes_other_errors_from_permission_errors(settings):
    def handler(request):
        return httpx.Response(500)

    client = _client(settings, handler)
    result = await check_page_capability(client, "site1")
    await client.aclose()

    assert result.available is False
    assert result.http_status == 500
    assert "Sites.Read.All" not in result.detail  # this is a different failure mode, shouldn't get the permission message


# --- content normalization -----------------------------------------------

def test_normalize_page_content_strips_html_and_includes_template_sections():
    detail = _page_detail(html="<p>Hello&nbsp;<b>world</b></p><script>evil()</script>")
    text = normalize_page_content(detail)

    assert "TITLE: My Page" in text
    assert "DESCRIPTION: A test page" in text
    assert "WEB URL:" in text
    assert "Hello" in text and "world" in text
    assert "<p>" not in text and "<b>" not in text


# --- new/modified/unchanged classification -------------------------------

@pytest.mark.asyncio
async def test_new_page_is_added(settings):
    def handler(request):
        url = str(request.url)
        if "microsoft.graph.sitePage?expand=canvasLayout" in url:
            return httpx.Response(200, json=_page_detail())
        raise AssertionError(f"unexpected url {url}")

    repo = InMemoryDocumentRepository()
    client = _client(settings, handler)
    outcome = await process_page(client, "site1", _page_summary(), repo)
    await client.aclose()

    from app.sync.manager import ProcessingOutcome
    assert outcome == ProcessingOutcome.added
    doc = await repo.get_by_sharepoint_item_id(SourceType.page, "page-1")
    assert doc.name == "My Page"
    assert doc.extension == ".aspx"


@pytest.mark.asyncio
async def test_unchanged_page_content_is_not_reprocessed(settings):
    def handler(request):
        return httpx.Response(200, json=_page_detail())

    from app.sync.manager import ProcessingOutcome
    repo = InMemoryDocumentRepository()
    client = _client(settings, handler)

    first = await process_page(client, "site1", _page_summary(), repo)
    second = await process_page(client, "site1", _page_summary(), repo)
    await client.aclose()

    assert first == ProcessingOutcome.added
    assert second == ProcessingOutcome.unchanged


@pytest.mark.asyncio
async def test_modified_page_content_is_detected(settings):
    responses = iter([_page_detail(html="<p>version one</p>"), _page_detail(html="<p>version two changed</p>")])

    def handler(request):
        return httpx.Response(200, json=next(responses))

    from app.sync.manager import ProcessingOutcome
    repo = InMemoryDocumentRepository()
    client = _client(settings, handler)

    first = await process_page(client, "site1", _page_summary(), repo)
    second = await process_page(client, "site1", _page_summary(), repo)
    await client.aclose()

    assert first == ProcessingOutcome.added
    assert second == ProcessingOutcome.modified


# --- deletion via full-listing diff (no native page delta exists) --------

@pytest.mark.asyncio
async def test_page_missing_from_listing_is_marked_deleted(settings):
    def handler(request):
        url = str(request.url)
        if url.endswith("/sites/site1/pages/microsoft.graph.sitePage"):
            # Only page-2 exists now -- page-1 has "disappeared".
            return httpx.Response(200, json={"value": [_page_summary("page-2", "Still Here")]})
        if "page-2" in url and "expand=canvasLayout" in url:
            return httpx.Response(200, json=_page_detail("page-2", "Still Here"))
        raise AssertionError(f"unexpected url {url}")

    repo = InMemoryDocumentRepository()
    # Pre-seed page-1 as previously tracked and still active.
    from app.sync.state import DocumentUpsert
    from app.db.models import ProcessingStatus
    await repo.upsert(DocumentUpsert(
        source_type=SourceType.page, sharepoint_item_id="page-1", drive_id=None, site_id="site1",
        name="Old Page", extension=".aspx", path=None, sharepoint_url="u", parent_sharepoint_item_id=None,
        modified_at=None, etag=None, size=10, content_hash="abc", processing_status=ProcessingStatus.pending,
    ))

    client = _client(settings, handler)
    counters = await run_page_sync(client, "site1", repo)
    await client.aclose()

    assert counters.items_deleted == 1
    assert counters.items_added == 1  # page-2 is new
    old_doc = await repo.get_by_sharepoint_item_id(SourceType.page, "page-1")
    assert old_doc.is_deleted is True


@pytest.mark.asyncio
async def test_page_listing_failure_is_recorded_not_raised(settings):
    def handler(request):
        return httpx.Response(503)

    repo = InMemoryDocumentRepository()
    client = _client(settings, handler)
    counters = await run_page_sync(client, "site1", repo)
    await client.aclose()

    assert counters.items_failed == 1
    assert counters.items_detected == 0


@pytest.mark.asyncio
async def test_unchanged_page_left_pending_by_earlier_sync_gets_chunked(settings):
    """Pages synced before chunking was wired in must not stay PENDING forever."""
    def handler(request):
        return httpx.Response(200, json=_page_detail())

    from app.sync.manager import ProcessingOutcome
    repo = InMemoryDocumentRepository()
    chunk_repo = InMemoryChunkRepository()
    client = _client(settings, handler)

    await process_page(client, "site1", _page_summary(), repo)
    doc = await repo.get_by_sharepoint_item_id(SourceType.page, "page-1")
    assert doc.processing_status == ProcessingStatus.pending

    outcome = await process_page(client, "site1", _page_summary(), repo, chunker=Chunker(), chunk_repo=chunk_repo)
    await client.aclose()

    assert outcome == ProcessingOutcome.unchanged
    assert doc.processing_status == ProcessingStatus.chunked
    assert chunk_repo.get_chunks(doc.id)
