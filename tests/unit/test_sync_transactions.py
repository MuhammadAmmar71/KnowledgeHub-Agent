"""
Transaction-boundary regressions. The plain InMemory repositories treat
commit/rollback as no-ops, which hid a real bug: with a single commit at
the end of a sync run, one item's rollback silently discarded every item
processed before it -- and even the failing item's own document row.

The doubles here snapshot state on commit() and restore it on rollback(),
like a real database session does.
"""
import httpx
import pytest

from app.chunking.chunker import Chunker
from app.db.models import ProcessingStatus, SourceType
from app.processing.router import build_default_router
from app.sharepoint.client import GraphClient
from app.sharepoint.drives import DiscoveredFile, DiscoveryResult
from app.sync.manager import run_initial_sync
from app.sync.state import InMemoryChunkRepository, InMemoryDocumentRepository
from tests.conftest import FakeGraphAuth

_TRACKED_FIELDS = ("processing_status", "processing_error", "content_hash", "is_deleted")


class TransactionalDocumentRepository(InMemoryDocumentRepository):
    def __init__(self) -> None:
        super().__init__()
        self.commits = 0
        self._committed: dict = {}

    async def commit(self) -> None:
        self.commits += 1
        self._committed = {
            key: (doc, {f: getattr(doc, f) for f in _TRACKED_FIELDS})
            for key, doc in self._store.items()
        }

    async def rollback(self) -> None:
        self._store = {key: doc for key, (doc, _) in self._committed.items()}
        for doc, fields in self._committed.values():
            for name, value in fields.items():
                setattr(doc, name, value)


class TransactionalChunkRepository(InMemoryChunkRepository):
    """Fails replace_chunks for documents whose name contains fail_on."""

    def __init__(self, doc_repo: TransactionalDocumentRepository, fail_on: str) -> None:
        super().__init__()
        self._doc_repo = doc_repo
        self._fail_on = fail_on

    async def replace_chunks(self, document_id, chunks) -> None:
        doc = await self._doc_repo.get_by_id(document_id)
        if self._fail_on in doc.name:
            raise RuntimeError("simulated chunk write failure")
        await super().replace_chunks(document_id, chunks)


def _discovered(item_id: str, name: str) -> DiscoveredFile:
    return DiscoveredFile(
        item_id=item_id, name=name, extension=".md", size=10, web_url=f"https://x/{name}",
        parent_item_id=None, path=f"/{name}", etag=None, last_modified=None, is_supported=True,
    )


def _client(settings) -> GraphClient:
    def handler(request):
        return httpx.Response(200, content=f"# Doc\nContent of {request.url.path}".encode())
    return GraphClient(FakeGraphAuth(), settings, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_chunking_failure_does_not_discard_earlier_items_or_its_own_document(settings):
    doc_repo = TransactionalDocumentRepository()
    chunk_repo = TransactionalChunkRepository(doc_repo, fail_on="bad")
    discovery = DiscoveryResult(files=[
        _discovered("i1", "first.md"),
        _discovered("i2", "bad.md"),
        _discovered("i3", "third.md"),
    ])
    client = _client(settings)

    await run_initial_sync(
        client, "drive1", "site1", doc_repo, discovery=discovery,
        router=build_default_router(), chunker=Chunker(), chunk_repo=chunk_repo,
    )
    await client.aclose()

    first = await doc_repo.get_by_sharepoint_item_id(SourceType.file, "i1")
    bad = await doc_repo.get_by_sharepoint_item_id(SourceType.file, "i2")
    third = await doc_repo.get_by_sharepoint_item_id(SourceType.file, "i3")

    # Work finished before the failure survives the failure's rollback.
    assert first is not None and first.processing_status == ProcessingStatus.chunked
    # The failing document's row survives too, with an accurate failure state.
    assert bad is not None
    assert bad.processing_status == ProcessingStatus.chunking_failed
    assert "simulated chunk write failure" in bad.processing_error
    # And processing continued afterwards.
    assert third is not None and third.processing_status == ProcessingStatus.chunked


@pytest.mark.asyncio
async def test_failed_chunking_is_retried_on_next_sync(settings):
    doc_repo = TransactionalDocumentRepository()
    discovery = DiscoveryResult(files=[_discovered("i1", "bad.md")])
    client = _client(settings)
    wiring = dict(router=build_default_router(), chunker=Chunker())

    await run_initial_sync(
        client, "drive1", "site1", doc_repo, discovery=discovery,
        chunk_repo=TransactionalChunkRepository(doc_repo, fail_on="bad"), **wiring,
    )
    # Next run: storage healthy again, content unchanged.
    healthy_chunks = TransactionalChunkRepository(doc_repo, fail_on="<never>")
    await run_initial_sync(
        client, "drive1", "site1", doc_repo, discovery=discovery, chunk_repo=healthy_chunks, **wiring,
    )
    await client.aclose()

    doc = await doc_repo.get_by_sharepoint_item_id(SourceType.file, "i1")
    assert doc.processing_status == ProcessingStatus.chunked
    assert healthy_chunks.get_chunks(doc.id)
