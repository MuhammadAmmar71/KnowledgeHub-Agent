"""
The embedding stage (Phase 7): CHUNKED -> EMBEDDING -> INDEXED (spec
section 56).

Deliberately a separate pass over the database rather than a step inside
sync: sync leaves new/changed documents at CHUNKED, and this stage picks up
whatever needs embedding by status. That keeps sync and extraction
independent of the embedding provider (Rules 10/11), lets chunks from many
small documents share one model call, and makes retry automatic -- a
document that failed here stays EMBEDDING_FAILED and is simply selected
again next run, regardless of the delta cursor.

Work is committed per document group, so a failure (and its rollback) can
never discard documents that already finished in the same run.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from app.chunking.chunker import Chunk
from app.db.models import EMBEDDING_DIMENSIONS, Document, ProcessingStatus
from app.embeddings.base import EmbeddingProvider
from app.embeddings.text import build_embedding_text
from app.sync.state import ChunkRepository, DocumentRepository

logger = logging.getLogger("knowledge_agent.embeddings.indexer")

# EMBEDDING is included so a document left mid-stage by a crashed run gets
# picked up again instead of being stuck forever.
_PENDING_STATUSES = {ProcessingStatus.chunked, ProcessingStatus.embedding, ProcessingStatus.embedding_failed}


class EmbeddingDimensionMismatchError(RuntimeError):
    pass


@dataclass
class EmbeddingRunCounters:
    documents_indexed: int = 0
    documents_failed: int = 0
    chunks_embedded: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class _DocumentRef:
    # Plain values, not the ORM Document: a rollback expires every ORM
    # object in the session, and touching an expired attribute under an
    # async session raises instead of lazily reloading.
    id: uuid.UUID
    name: str


@dataclass
class _PendingDocument:
    document: _DocumentRef
    chunks: list[Chunk]


async def _select_documents(
    doc_repo: DocumentRepository, chunk_repo: ChunkRepository, model_name: str, reembed_all: bool
) -> list[_DocumentRef]:
    statuses = set(_PENDING_STATUSES)
    if reembed_all:
        statuses.add(ProcessingStatus.indexed)
    selected: dict[uuid.UUID, Document] = {
        doc.id: doc for doc in await doc_repo.list_by_processing_status(statuses)
    }

    if not reembed_all:
        # Model change (spec section 35): anything embedded by a different
        # model is stale, even though its status says INDEXED.
        for document_id in await chunk_repo.document_ids_with_other_embedding_model(model_name):
            if document_id in selected:
                continue
            doc = await doc_repo.get_by_id(document_id)
            if doc is not None and not doc.is_deleted:
                selected[doc.id] = doc

    return [_DocumentRef(id=doc.id, name=doc.name) for doc in selected.values()]


async def _embed_texts(provider: EmbeddingProvider, texts: list[str], batch_size: int) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        vectors.extend(await provider.embed(texts[start:start + batch_size]))
    if len(vectors) != len(texts):
        raise RuntimeError(f"provider returned {len(vectors)} vectors for {len(texts)} texts")
    return vectors


async def _mark_failed(
    doc_repo: DocumentRepository, pending: _PendingDocument, exc: Exception, counters: EmbeddingRunCounters
) -> None:
    doc = pending.document
    logger.error("embedding_failed document_id=%s name=%s error=%s", doc.id, doc.name, exc)
    counters.documents_failed += 1
    counters.errors.append(f"{doc.name} ({doc.id}): {exc}")
    try:
        await doc_repo.rollback()
        await doc_repo.mark_processing_status(doc.id, ProcessingStatus.embedding_failed, str(exc)[:2000])
        await doc_repo.commit()
    except Exception:
        logger.exception("failed_to_record_embedding_failure document_id=%s", doc.id)
        await doc_repo.rollback()


async def _persist(
    doc_repo: DocumentRepository,
    chunk_repo: ChunkRepository,
    pending: _PendingDocument,
    vectors: list[list[float]],
    model_name: str,
    counters: EmbeddingRunCounters,
) -> None:
    doc = pending.document
    try:
        await chunk_repo.set_embeddings(
            doc.id, {chunk.id: vector for chunk, vector in zip(pending.chunks, vectors)}, model_name
        )
        await doc_repo.mark_processing_status(doc.id, ProcessingStatus.indexed)
        await doc_repo.commit()
    except Exception as exc:
        await _mark_failed(doc_repo, pending, exc, counters)
        return
    counters.documents_indexed += 1
    counters.chunks_embedded += len(pending.chunks)


async def _process_group(
    group: list[_PendingDocument],
    doc_repo: DocumentRepository,
    chunk_repo: ChunkRepository,
    provider: EmbeddingProvider,
    batch_size: int,
    counters: EmbeddingRunCounters,
) -> None:
    texts = [
        build_embedding_text(chunk.content, chunk.metadata)
        for pending in group
        for chunk in pending.chunks
    ]
    try:
        vectors = await _embed_texts(provider, texts, batch_size)
    except Exception as exc:
        if len(group) > 1:
            # One document's chunks can sink a shared batch; retry each
            # document alone so only the actual culprit is marked failed.
            for pending in group:
                await _process_group([pending], doc_repo, chunk_repo, provider, batch_size, counters)
        else:
            await _mark_failed(doc_repo, group[0], exc, counters)
        return

    offset = 0
    for pending in group:
        count = len(pending.chunks)
        await _persist(doc_repo, chunk_repo, pending, vectors[offset:offset + count], provider.model_name, counters)
        offset += count


async def embed_pending_documents(
    doc_repo: DocumentRepository,
    chunk_repo: ChunkRepository,
    provider: EmbeddingProvider,
    batch_size: int,
    reembed_all: bool = False,
) -> EmbeddingRunCounters:
    """
    Embeds every non-deleted document that needs it: CHUNKED,
    EMBEDDING_FAILED, interrupted mid-EMBEDDING, or INDEXED by a different
    model than provider.model_name. reembed_all=True also includes every
    INDEXED document (full re-embedding, spec section 35).
    """
    if provider.dimensions != EMBEDDING_DIMENSIONS:
        raise EmbeddingDimensionMismatchError(
            f"embedding model '{provider.model_name}' produces {provider.dimensions}-dim vectors, "
            f"but the chunks.embedding column is vector({EMBEDDING_DIMENSIONS}); "
            "changing dimensions requires a new migration"
        )

    counters = EmbeddingRunCounters()
    documents = await _select_documents(doc_repo, chunk_repo, provider.model_name, reembed_all)
    logger.info("embedding_run_started documents=%d model=%s", len(documents), provider.model_name)

    group: list[_PendingDocument] = []
    group_chunks = 0

    for doc in documents:
        chunks = await chunk_repo.load_chunks(doc.id)
        if not chunks:
            # Empty extraction -- nothing to embed, but nothing left to do either.
            await doc_repo.mark_processing_status(doc.id, ProcessingStatus.indexed)
            await doc_repo.commit()
            counters.documents_indexed += 1
            continue

        await doc_repo.mark_processing_status(doc.id, ProcessingStatus.embedding)
        group.append(_PendingDocument(doc, chunks))
        group_chunks += len(chunks)

        if group_chunks >= batch_size:
            await _process_group(group, doc_repo, chunk_repo, provider, batch_size, counters)
            group, group_chunks = [], 0

    if group:
        await _process_group(group, doc_repo, chunk_repo, provider, batch_size, counters)

    logger.info(
        "embedding_run_completed indexed=%d failed=%d chunks=%d",
        counters.documents_indexed, counters.documents_failed, counters.chunks_embedded,
    )
    return counters
