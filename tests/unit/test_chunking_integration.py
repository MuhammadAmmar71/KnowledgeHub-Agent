import httpx
import pytest

from app.chunking.chunker import Chunker
from app.db.models import ProcessingStatus, SourceType
from app.processing.router import build_default_router
from app.sharepoint.client import GraphClient
from app.sync.manager import process_discovered_file, process_page
from app.sync.state import InMemoryChunkRepository, InMemoryDocumentRepository
from tests.conftest import FakeGraphAuth


def _client(settings, handler) -> GraphClient:
    return GraphClient(FakeGraphAuth(), settings, transport=httpx.MockTransport(handler))


def _discovered(item_id="item-1", name="test.md"):
    from app.sharepoint.drives import DiscoveredFile
    return DiscoveredFile(
        item_id=item_id, name=name, extension=".md", size=42,
        web_url=f"https://example.sharepoint.com/{name}", parent_item_id=None,
        path=f"/{name}", etag="\"v1\"", last_modified="2026-01-01T00:00:00Z",
        is_supported=True,
    )


@pytest.mark.asyncio
async def test_new_file_gets_chunked_when_wiring_is_supplied(settings):
    def handler(request):
        return httpx.Response(200, content=b"# Heading One\nSome real content for chunking.")

    doc_repo = InMemoryDocumentRepository()
    chunk_repo = InMemoryChunkRepository()
    router = build_default_router()
    chunker = Chunker()
    client = _client(settings, handler)

    outcome = await process_discovered_file(
        client, "drive1", "site1", _discovered(), doc_repo, router=router, chunker=chunker, chunk_repo=chunk_repo,
    )
    await client.aclose()

    doc = await doc_repo.get_by_sharepoint_item_id(SourceType.file, "item-1")
    chunks = chunk_repo.get_chunks(doc.id)

    assert outcome.value == "added"
    assert doc.processing_status == ProcessingStatus.chunked
    assert len(chunks) >= 1
    assert chunks[0].metadata["sharepoint_item_id"] == "item-1"
    assert chunks[0].metadata["heading"] == "Heading One"


@pytest.mark.asyncio
async def test_unchanged_file_is_not_rechunked(settings):
    """
    Idempotency extends to chunking: an unchanged file must not trigger a
    second extract+chunk+replace cycle -- that would be wasted work on
    every single sync run forever.
    """
    def handler(request):
        return httpx.Response(200, content=b"Same content every time.")

    doc_repo = InMemoryDocumentRepository()
    chunk_repo = InMemoryChunkRepository()
    router = build_default_router()
    chunker = Chunker()
    client = _client(settings, handler)

    first = await process_discovered_file(
        client, "drive1", "site1", _discovered(), doc_repo, router=router, chunker=chunker, chunk_repo=chunk_repo,
    )
    doc_after_first = await doc_repo.get_by_sharepoint_item_id(SourceType.file, "item-1")
    chunks_after_first = chunk_repo.get_chunks(doc_after_first.id)

    second = await process_discovered_file(
        client, "drive1", "site1", _discovered(), doc_repo, router=router, chunker=chunker, chunk_repo=chunk_repo,
    )
    await client.aclose()

    doc_after_second = await doc_repo.get_by_sharepoint_item_id(SourceType.file, "item-1")
    chunks_after_second = chunk_repo.get_chunks(doc_after_second.id)

    assert first.value == "added"
    assert second.value == "unchanged"
    assert [c.id for c in chunks_after_first] == [c.id for c in chunks_after_second]


@pytest.mark.asyncio
async def test_modified_file_gets_rechunked_with_new_chunk_ids(settings):
    responses = iter([b"# V1\nOriginal content.", b"# V2\nCompletely different content now."])

    def handler(request):
        return httpx.Response(200, content=next(responses))

    doc_repo = InMemoryDocumentRepository()
    chunk_repo = InMemoryChunkRepository()
    router = build_default_router()
    chunker = Chunker()
    client = _client(settings, handler)

    await process_discovered_file(
        client, "drive1", "site1", _discovered(), doc_repo, router=router, chunker=chunker, chunk_repo=chunk_repo,
    )
    doc1 = await doc_repo.get_by_sharepoint_item_id(SourceType.file, "item-1")
    chunks1 = chunk_repo.get_chunks(doc1.id)

    await process_discovered_file(
        client, "drive1", "site1", _discovered(), doc_repo, router=router, chunker=chunker, chunk_repo=chunk_repo,
    )
    doc2 = await doc_repo.get_by_sharepoint_item_id(SourceType.file, "item-1")
    chunks2 = chunk_repo.get_chunks(doc2.id)
    await client.aclose()

    assert doc2.processing_status == ProcessingStatus.chunked
    assert {c.id for c in chunks1}.isdisjoint({c.id for c in chunks2})  # old chunks fully replaced, new IDs
    # Headings are lifted out of chunk content into metadata, so check both.
    assert any(c.metadata.get("heading") == "V2" for c in chunks2)
    assert any("Completely different content now." in c.content for c in chunks2)
    assert not any("Original content." in c.content for c in chunks2)


@pytest.mark.asyncio
async def test_chunking_not_attempted_when_wiring_omitted(settings):
    """Backward compatibility: Phase 2-4 callers that don't pass router/chunker/chunk_repo still work unchanged."""
    def handler(request):
        return httpx.Response(200, content=b"content")

    doc_repo = InMemoryDocumentRepository()
    client = _client(settings, handler)
    outcome = await process_discovered_file(client, "drive1", "site1", _discovered(), doc_repo)
    await client.aclose()

    doc = await doc_repo.get_by_sharepoint_item_id(SourceType.file, "item-1")
    assert outcome.value == "added"
    assert doc.processing_status == ProcessingStatus.pending  # never touched by chunking


@pytest.mark.asyncio
async def test_page_gets_chunked_when_wiring_is_supplied(settings):
    def handler(request):
        return httpx.Response(200, json={
            "id": "page-1", "title": "My Page", "description": "desc",
            "webUrl": "https://example.sharepoint.com/page-1",
            "canvasLayout": {"horizontalSections": [{"columns": [{"webparts": [
                {"innerHtml": "<p>Real page content for chunking.</p>"}
            ]}]}]},
        })

    doc_repo = InMemoryDocumentRepository()
    chunk_repo = InMemoryChunkRepository()
    chunker = Chunker()
    client = _client(settings, handler)

    page_summary = {"id": "page-1", "title": "My Page", "webUrl": "u", "lastModifiedDateTime": "2026-01-01T00:00:00Z"}
    outcome = await process_page(client, "site1", page_summary, doc_repo, chunker=chunker, chunk_repo=chunk_repo)
    await client.aclose()

    doc = await doc_repo.get_by_sharepoint_item_id(SourceType.page, "page-1")
    chunks = chunk_repo.get_chunks(doc.id)

    assert outcome.value == "added"
    assert doc.processing_status == ProcessingStatus.chunked
    assert len(chunks) >= 1
    assert any("Real page content" in c.content for c in chunks)


@pytest.mark.asyncio
async def test_unchanged_file_left_pending_by_earlier_sync_gets_chunked(settings):
    """
    Files synced before chunking was wired in sit at PENDING with a matching
    hash. An unchanged hash must not leave them stuck there forever.
    """
    def handler(request):
        return httpx.Response(200, content=b"# Heading\nSynced before Phase 6.")

    doc_repo = InMemoryDocumentRepository()
    chunk_repo = InMemoryChunkRepository()
    client = _client(settings, handler)

    # First sync without chunking wiring (pre-Phase-6 behavior) -> PENDING.
    await process_discovered_file(client, "drive1", "site1", _discovered(), doc_repo)
    doc = await doc_repo.get_by_sharepoint_item_id(SourceType.file, "item-1")
    assert doc.processing_status == ProcessingStatus.pending

    outcome = await process_discovered_file(
        client, "drive1", "site1", _discovered(), doc_repo,
        router=build_default_router(), chunker=Chunker(), chunk_repo=chunk_repo,
    )
    await client.aclose()

    assert outcome.value == "unchanged"
    assert doc.processing_status == ProcessingStatus.chunked
    assert chunk_repo.get_chunks(doc.id)


@pytest.mark.asyncio
async def test_file_recovering_from_download_failure_with_same_content_gets_chunked(settings):
    """A failed download keeps the old hash, so the next good download of the same bytes looks unchanged."""
    responses = iter([
        httpx.Response(200, content=b"Stable content."),
        httpx.Response(404),
        httpx.Response(200, content=b"Stable content."),
    ])

    def handler(request):
        return next(responses)

    doc_repo = InMemoryDocumentRepository()
    chunk_repo = InMemoryChunkRepository()
    wiring = dict(router=build_default_router(), chunker=Chunker(), chunk_repo=chunk_repo)
    client = _client(settings, handler)

    await process_discovered_file(client, "drive1", "site1", _discovered(), doc_repo, **wiring)
    await process_discovered_file(client, "drive1", "site1", _discovered(), doc_repo, **wiring)
    doc = await doc_repo.get_by_sharepoint_item_id(SourceType.file, "item-1")
    assert doc.processing_status == ProcessingStatus.download_failed

    await process_discovered_file(client, "drive1", "site1", _discovered(), doc_repo, **wiring)
    await client.aclose()

    assert doc.processing_status == ProcessingStatus.chunked
