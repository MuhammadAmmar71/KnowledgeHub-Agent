"""
Unit tests for the parts of retrieval that don't need Postgres: rank
fusion, filter -> SQL translation, and query-side embedding. The SQL
itself (pgvector distance, full-text match) is exercised against a real
database via the CLI, not here.
"""
import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.db.models import SourceType
from app.retrieval.search import RRF_K, SearchFilters, _base_query, reciprocal_rank_fusion


def _ids(n):
    return [uuid.uuid4() for _ in range(n)]


def test_rrf_item_in_both_lists_beats_top_of_one_list():
    a, b, c = _ids(3)
    fused = reciprocal_rank_fusion([[a, b], [c, b]])
    assert [item for item, _ in fused][0] == b


def test_rrf_scores_follow_formula():
    a, b = _ids(2)
    fused = dict(reciprocal_rank_fusion([[a, b], [b]]))
    assert fused[a] == pytest.approx(1 / (RRF_K + 1))
    assert fused[b] == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))


def test_rrf_ties_keep_first_list_order():
    a, b = _ids(2)
    fused = reciprocal_rank_fusion([[a], [b]])
    assert [item for item, _ in fused] == [a, b]


def test_rrf_handles_empty_lists():
    a = uuid.uuid4()
    assert reciprocal_rank_fusion([[], []]) == []
    assert [i for i, _ in reciprocal_rank_fusion([[a], []])] == [a]


def _sql(filters: SearchFilters) -> str:
    stmt = _base_query("model-x", filters)
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def test_base_query_always_excludes_deleted_and_other_models():
    sql = _sql(SearchFilters())
    assert "documents.is_deleted IS false" in sql
    assert "chunks.embedding IS NOT NULL" in sql
    assert "chunks.embedding_model = 'model-x'" in sql


def test_base_query_applies_filters():
    sql = _sql(SearchFilters(source_type=SourceType.page, extensions=[".MD", ".txt"], path_contains="02_Dev"))
    assert "documents.source_type = 'page'" in sql
    assert "documents.extension IN ('.md', '.txt')" in sql
    # "_" is escaped so it matches a literal underscore, not any character.
    assert "02/_Dev" in sql and "ESCAPE '/'" in sql


@pytest.mark.asyncio
async def test_query_embedding_gets_instruction_prefix_documents_do_not(monkeypatch):
    from app.embeddings.sentence_transformer import SentenceTransformerProvider

    seen: list[list[str]] = []
    provider = SentenceTransformerProvider("m", query_instruction="Query: ")
    monkeypatch.setattr(provider, "_encode", lambda texts: seen.append(texts) or [[0.0]] * len(texts))

    await provider.embed_query("find docker steps")
    await provider.embed(["a passage"])

    assert seen == [["Query: find docker steps"], ["a passage"]]
