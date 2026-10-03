import pytest

from app.chunking.chunker import Chunker
from app.db.models import ProcessingStatus, SourceType
from app.embeddings.indexer import EmbeddingDimensionMismatchError, embed_pending_documents
from app.sync.state import DocumentUpsert, InMemoryChunkRepository, InMemoryDocumentRepository
from tests.conftest import FakeEmbeddingProvider


async def _add_chunked_document(doc_repo, chunk_repo, item_id: str, text: str, content_hash: str | None = None):
    doc = await doc_repo.upsert(DocumentUpsert(
        source_type=SourceType.file, sharepoint_item_id=item_id, drive_id="d", site_id="s",
        name=f"{item_id}.md", extension=".md", path=f"/{item_id}.md", sharepoint_url="u",
        parent_sharepoint_item_id=None, modified_at=None, etag=None, size=len(text),
        content_hash=content_hash or f"hash-{item_id}", processing_status=ProcessingStatus.chunked,
    ))
    chunks = Chunker(chunk_size=60, chunk_overlap=0).chunk(text, doc.id, doc.content_hash, {"name": doc.name})
    await chunk_repo.replace_chunks(doc.id, chunks)
    return doc


@pytest.fixture
def repos():
    return InMemoryDocumentRepository(), InMemoryChunkRepository()


@pytest.mark.asyncio
async def test_chunked_document_becomes_indexed_with_every_chunk_embedded(repos):
    doc_repo, chunk_repo = repos
    doc = await _add_chunked_document(doc_repo, chunk_repo, "a", "# One\nFirst section.\n# Two\nSecond section.")
    provider = FakeEmbeddingProvider()

    counters = await embed_pending_documents(doc_repo, chunk_repo, provider, batch_size=32)

    chunks = chunk_repo.get_chunks(doc.id)
    assert doc.processing_status == ProcessingStatus.indexed
    assert counters.documents_indexed == 1
    assert counters.chunks_embedded == len(chunks) == 2
    for chunk in chunks:
        vector, model = chunk_repo.embeddings[chunk.id]
        assert len(vector) == 384
        assert model == "fake-model"
    # Section context is embedded even though it's not in stored content.
    assert provider.calls[0][0].startswith("a.md > One\n")


@pytest.mark.asyncio
async def test_one_failing_document_does_not_block_others_and_is_retried(repos):
    doc_repo, chunk_repo = repos
    good = await _add_chunked_document(doc_repo, chunk_repo, "good", "Perfectly fine content.")
    bad = await _add_chunked_document(doc_repo, chunk_repo, "bad", "This one contains POISON.")

    counters = await embed_pending_documents(
        doc_repo, chunk_repo, FakeEmbeddingProvider(fail_on="POISON"), batch_size=32
    )

    assert good.processing_status == ProcessingStatus.indexed
    assert bad.processing_status == ProcessingStatus.embedding_failed
    assert "POISON" in bad.processing_error
    assert counters.documents_indexed == 1 and counters.documents_failed == 1

    # Next run (provider healthy again) picks the failed document back up.
    retry = await embed_pending_documents(doc_repo, chunk_repo, FakeEmbeddingProvider(), batch_size=32)
    assert bad.processing_status == ProcessingStatus.indexed
    assert retry.documents_indexed == 1


@pytest.mark.asyncio
async def test_indexed_document_with_same_model_is_not_re_embedded(repos):
    doc_repo, chunk_repo = repos
    await _add_chunked_document(doc_repo, chunk_repo, "a", "Some content.")
    await embed_pending_documents(doc_repo, chunk_repo, FakeEmbeddingProvider(), batch_size=32)

    second_provider = FakeEmbeddingProvider()
    counters = await embed_pending_documents(doc_repo, chunk_repo, second_provider, batch_size=32)

    assert second_provider.calls == []
    assert counters.documents_indexed == 0


@pytest.mark.asyncio
async def test_model_change_triggers_re_embedding(repos):
    doc_repo, chunk_repo = repos
    doc = await _add_chunked_document(doc_repo, chunk_repo, "a", "Some content.")
    await embed_pending_documents(doc_repo, chunk_repo, FakeEmbeddingProvider("model-v1"), batch_size=32)

    counters = await embed_pending_documents(doc_repo, chunk_repo, FakeEmbeddingProvider("model-v2"), batch_size=32)

    assert counters.documents_indexed == 1
    assert {model for _, model in chunk_repo.embeddings.values()} == {"model-v2"}
    assert doc.processing_status == ProcessingStatus.indexed


@pytest.mark.asyncio
async def test_reembed_all_includes_up_to_date_documents(repos):
    doc_repo, chunk_repo = repos
    await _add_chunked_document(doc_repo, chunk_repo, "a", "Some content.")
    await embed_pending_documents(doc_repo, chunk_repo, FakeEmbeddingProvider(), batch_size=32)

    provider = FakeEmbeddingProvider()
    counters = await embed_pending_documents(doc_repo, chunk_repo, provider, batch_size=32, reembed_all=True)

    assert counters.documents_indexed == 1
    assert provider.calls


@pytest.mark.asyncio
async def test_batches_never_exceed_batch_size_and_span_documents(repos):
    doc_repo, chunk_repo = repos
    for i in range(5):
        await _add_chunked_document(doc_repo, chunk_repo, f"d{i}", f"Small document number {i}.")
    provider = FakeEmbeddingProvider()

    counters = await embed_pending_documents(doc_repo, chunk_repo, provider, batch_size=2)

    assert counters.documents_indexed == 5
    assert all(len(call) <= 2 for call in provider.calls)
    # Single-chunk documents are grouped: one call covers two documents.
    assert any(len(call) == 2 for call in provider.calls)


@pytest.mark.asyncio
async def test_dimension_mismatch_raises_before_any_writes(repos):
    doc_repo, chunk_repo = repos
    doc = await _add_chunked_document(doc_repo, chunk_repo, "a", "Some content.")
    provider = FakeEmbeddingProvider(dimensions=768)

    with pytest.raises(EmbeddingDimensionMismatchError):
        await embed_pending_documents(doc_repo, chunk_repo, provider, batch_size=32)

    assert provider.calls == []
    assert doc.processing_status == ProcessingStatus.chunked
    assert chunk_repo.embeddings == {}


@pytest.mark.asyncio
async def test_deleted_documents_are_skipped(repos):
    doc_repo, chunk_repo = repos
    doc = await _add_chunked_document(doc_repo, chunk_repo, "a", "Some content.")
    await doc_repo.mark_deleted(SourceType.file, "a")
    provider = FakeEmbeddingProvider()

    counters = await embed_pending_documents(doc_repo, chunk_repo, provider, batch_size=32)

    assert provider.calls == []
    assert counters.documents_indexed == 0
    assert doc.processing_status == ProcessingStatus.chunked


@pytest.mark.asyncio
async def test_document_with_no_chunks_is_marked_indexed(repos):
    doc_repo, chunk_repo = repos
    doc = await _add_chunked_document(doc_repo, chunk_repo, "empty", "")

    counters = await embed_pending_documents(doc_repo, chunk_repo, FakeEmbeddingProvider(), batch_size=32)

    assert doc.processing_status == ProcessingStatus.indexed
    assert counters.documents_indexed == 1


@pytest.mark.asyncio
async def test_rechunked_document_drops_old_embeddings_and_is_re_embedded(repos):
    """A modified file gets fresh chunks (sync sets it back to CHUNKED); old vectors must not linger."""
    doc_repo, chunk_repo = repos
    doc = await _add_chunked_document(doc_repo, chunk_repo, "a", "Version one.")
    await embed_pending_documents(doc_repo, chunk_repo, FakeEmbeddingProvider(), batch_size=32)
    old_ids = {c.id for c in chunk_repo.get_chunks(doc.id)}

    doc = await _add_chunked_document(doc_repo, chunk_repo, "a", "Version two, quite different.", "hash-a-v2")
    assert old_ids.isdisjoint(chunk_repo.embeddings)

    await embed_pending_documents(doc_repo, chunk_repo, FakeEmbeddingProvider(), batch_size=32)
    assert doc.processing_status == ProcessingStatus.indexed
    assert {c.id for c in chunk_repo.get_chunks(doc.id)} == set(chunk_repo.embeddings)
