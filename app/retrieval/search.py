"""
Chunk retrieval (spec sections 36/37): semantic similarity over pgvector,
optionally fused with a Postgres full-text keyword signal, with source
metadata filters and top-k.

Hybrid mode runs both searches independently and merges them with
Reciprocal Rank Fusion. RRF only looks at rank positions, so it needs no
tuning to reconcile cosine similarity with ts_rank scores (which live on
unrelated scales), and a chunk found by both searches naturally rises to
the top. The keyword side matters for exact terms embeddings blur: resource
names, error codes, CLI flags.

Results carry name / URL / path / section from the documents table (not the
chunk's metadata snapshot), so citations always show the current webUrl
(spec sections 37/39). Deleted documents are never returned, and only
chunks embedded by the active model are compared -- vectors from different
models aren't comparable.
"""
from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk as ChunkModel
from app.db.models import Document, SourceType
from app.embeddings.base import EmbeddingProvider
from app.embeddings.text import section_label

# Standard RRF constant (Cormack et al.); dampens the advantage of rank 1
# over rank 2 so one list can't dominate the fusion on its own.
RRF_K = 60
# How many candidates each side contributes before fusion, per result asked for.
CANDIDATE_MULTIPLIER = 5


class SearchMode(str, enum.Enum):
    hybrid = "hybrid"
    semantic = "semantic"


@dataclass
class SearchFilters:
    source_type: SourceType | None = None
    extensions: list[str] = field(default_factory=list)
    path_contains: str | None = None


@dataclass
class SearchResult:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    chunk_index: int
    content: str
    name: str
    sharepoint_url: str | None
    path: str | None
    source_type: str
    section: str | None
    score: float
    # Per-signal detail, None when that search didn't return this chunk.
    similarity: float | None = None
    semantic_rank: int | None = None
    keyword_rank: int | None = None


def reciprocal_rank_fusion(rankings: list[list[uuid.UUID]], k: int = RRF_K) -> list[tuple[uuid.UUID, float]]:
    """
    Merges ranked id lists (best first) into one ranking by
    sum(1 / (k + rank)). Ties keep first-seen order, so the earlier list
    (semantic) wins ties.
    """
    scores: dict[uuid.UUID, float] = {}
    for ranking in rankings:
        for rank, item_id in enumerate(ranking, start=1):
            scores[item_id] = scores.get(item_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: pair[1], reverse=True)


def _base_query(model_name: str, filters: SearchFilters) -> Select:
    stmt = (
        select(ChunkModel, Document)
        .join(Document, Document.id == ChunkModel.document_id)
        .where(
            Document.is_deleted.is_(False),
            ChunkModel.embedding.is_not(None),
            ChunkModel.embedding_model == model_name,
        )
    )
    if filters.source_type is not None:
        stmt = stmt.where(Document.source_type == filters.source_type)
    if filters.extensions:
        stmt = stmt.where(Document.extension.in_([e.lower() for e in filters.extensions]))
    if filters.path_contains:
        # autoescape: "_" and "%" in user input (e.g. "02_Development")
        # match literally instead of acting as LIKE wildcards.
        stmt = stmt.where(Document.path.icontains(filters.path_contains, autoescape=True))
    return stmt


async def _semantic_candidates(
    session: AsyncSession, query_vector: list[float], model_name: str, filters: SearchFilters, limit: int
) -> list[tuple[ChunkModel, Document, float]]:
    # pgvector >= 0.8: keep scanning the HNSW index until enough rows pass
    # the WHERE filters. Without this, a filtered query only sees the
    # ef_search nearest neighbours *before* filtering and can come back
    # with fewer results than asked for. relaxed_order may emit slightly
    # out-of-order rows, hence the re-sort below. SET LOCAL scopes it to
    # this transaction.
    await session.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
    distance = ChunkModel.embedding.cosine_distance(query_vector)
    stmt = _base_query(model_name, filters).add_columns(distance.label("distance")).order_by(distance).limit(limit)
    rows = (await session.execute(stmt)).all()
    ordered = sorted(rows, key=lambda row: row.distance)
    return [(row[0], row[1], 1.0 - float(row.distance)) for row in ordered]


async def _keyword_candidates(
    session: AsyncSession, query: str, model_name: str, filters: SearchFilters, limit: int
) -> list[tuple[ChunkModel, Document]]:
    # websearch_to_tsquery accepts free text (quotes, OR, -exclusions)
    # and never raises on odd punctuation, unlike to_tsquery.
    tsquery = func.websearch_to_tsquery("english", query)
    rank = func.ts_rank_cd(ChunkModel.content_tsv, tsquery)
    stmt = (
        _base_query(model_name, filters)
        .where(ChunkModel.content_tsv.op("@@")(tsquery))
        .order_by(rank.desc())
        .limit(limit)
    )
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


def _to_result(chunk: ChunkModel, document: Document, score: float) -> SearchResult:
    source_type = document.source_type
    return SearchResult(
        chunk_id=chunk.id,
        document_id=document.id,
        chunk_index=chunk.chunk_index,
        content=chunk.content,
        name=document.name,
        sharepoint_url=document.sharepoint_url,
        path=document.path,
        source_type=source_type.value if isinstance(source_type, SourceType) else str(source_type),
        section=section_label(chunk.metadata_ or {}),
        score=score,
    )


async def search_chunks(
    session: AsyncSession,
    provider: EmbeddingProvider,
    query: str,
    top_k: int = 5,
    mode: SearchMode = SearchMode.hybrid,
    filters: SearchFilters | None = None,
) -> list[SearchResult]:
    filters = filters or SearchFilters()
    query = query.strip()
    if not query or top_k <= 0:
        return []

    candidate_limit = max(top_k * CANDIDATE_MULTIPLIER, 20)
    query_vector = await provider.embed_query(query)
    semantic = await _semantic_candidates(session, query_vector, provider.model_name, filters, candidate_limit)

    if mode == SearchMode.semantic:
        results = []
        for rank, (chunk, document, similarity) in enumerate(semantic[:top_k], start=1):
            result = _to_result(chunk, document, similarity)
            result.similarity, result.semantic_rank = similarity, rank
            results.append(result)
        return results

    keyword = await _keyword_candidates(session, query, provider.model_name, filters, candidate_limit)

    rows: dict[uuid.UUID, tuple[ChunkModel, Document]] = {}
    similarity_by_id: dict[uuid.UUID, float] = {}
    semantic_rank: dict[uuid.UUID, int] = {}
    keyword_rank: dict[uuid.UUID, int] = {}
    for rank, (chunk, document, similarity) in enumerate(semantic, start=1):
        rows[chunk.id] = (chunk, document)
        similarity_by_id[chunk.id] = similarity
        semantic_rank[chunk.id] = rank
    for rank, (chunk, document) in enumerate(keyword, start=1):
        rows.setdefault(chunk.id, (chunk, document))
        keyword_rank[chunk.id] = rank

    fused = reciprocal_rank_fusion([
        [chunk.id for chunk, _, _ in semantic],
        [chunk.id for chunk, _ in keyword],
    ])

    results = []
    for chunk_id, score in fused[:top_k]:
        chunk, document = rows[chunk_id]
        result = _to_result(chunk, document, score)
        result.similarity = similarity_by_id.get(chunk_id)
        result.semantic_rank = semantic_rank.get(chunk_id)
        result.keyword_rank = keyword_rank.get(chunk_id)
        results.append(result)
    return results
