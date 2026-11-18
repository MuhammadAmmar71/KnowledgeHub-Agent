import sys
import threading
import types
import uuid

import numpy as np
import pytest

import app.retrieval.search as search_module
from app.cli import build_parser, format_results
from app.llm.base import LLMResponse
from app.rag.answer import NO_CONTEXT_ANSWER, answer_question
from app.retrieval.rerank import CrossEncoderReranker
from app.retrieval.search import SearchResult, rerank_passage, rerank_results, search_chunks
from tests.conftest import FakeEmbeddingProvider


def _result(name, content="text", section=None, score=0.0):
    return SearchResult(
        chunk_id=uuid.uuid4(), document_id=uuid.uuid4(), chunk_index=0, content=content, name=name,
        sharepoint_url=f"https://sp/{name}", path=f"/{name}", source_type="file", section=section, score=score,
    )


class FakeReranker:
    """Scores passages by a name -> relevance table; records what it was shown."""

    model_name = "fake-reranker"

    def __init__(self, relevance: dict[str, float]):
        self.relevance = relevance
        self.calls: list[tuple[str, list[str]]] = []

    async def score(self, query, passages):
        self.calls.append((query, passages))
        return [next((v for k, v in self.relevance.items() if p.startswith(k)), 0.0) for p in passages]


# --- rerank_results ----------------------------------------------------------

@pytest.mark.asyncio
async def test_rerank_reorders_by_relevance_and_keeps_top_k():
    results = [_result("a.md"), _result("b.md"), _result("c.md")]
    reranked = await rerank_results(FakeReranker({"a.md": 0.1, "b.md": 0.9, "c.md": 0.5}), "q", results, top_k=2)

    assert [r.name for r in reranked] == ["b.md", "c.md"]
    assert reranked[0].rerank_score == reranked[0].score == 0.9


@pytest.mark.asyncio
async def test_min_relevance_can_drop_everything():
    results = [_result("a.md"), _result("b.md")]
    reranked = await rerank_results(FakeReranker({"a.md": 0.2, "b.md": 0.05}), "q", results, 5, min_relevance=0.3)
    assert reranked == []


@pytest.mark.asyncio
async def test_equal_relevance_keeps_retrieval_order():
    results = [_result("first.md"), _result("second.md")]
    reranked = await rerank_results(FakeReranker({}), "q", results, top_k=2)
    assert [r.name for r in reranked] == ["first.md", "second.md"]


def test_rerank_passage_includes_document_and_section_context():
    assert rerank_passage(_result("Guide.md", "Contributor", section="Required Permissions")) == (
        "Guide.md > Required Permissions\nContributor"
    )


# --- search_chunks pool sizing -----------------------------------------------

@pytest.fixture
def captured_pool(monkeypatch):
    calls = {}
    pool = [_result(f"d{i}.md") for i in range(30)]

    async def fake_search(session, provider, query, top_k, mode, filters):
        calls["top_k"] = top_k
        return pool[:top_k]

    monkeypatch.setattr(search_module, "_search_chunks", fake_search)
    return calls


@pytest.mark.asyncio
async def test_with_reranker_retrieves_candidate_pool_then_trims(captured_pool):
    reranker = FakeReranker({"d29.md": 0.99})
    results = await search_chunks(None, FakeEmbeddingProvider(), "q", top_k=3, reranker=reranker, rerank_candidates=30)

    assert captured_pool["top_k"] == 30
    assert len(results) == 3
    assert results[0].name == "d29.md"  # promoted from the bottom of the pool


@pytest.mark.asyncio
async def test_without_reranker_retrieves_only_top_k(captured_pool):
    results = await search_chunks(None, FakeEmbeddingProvider(), "q", top_k=3)
    assert captured_pool["top_k"] == 3
    assert all(r.rerank_score is None for r in results)


# --- RAG: relevance cut-off means no LLM call ----------------------------------

class _FakeLLM:
    def __init__(self):
        self.calls = []

    async def generate(self, instructions, user_input):
        self.calls.append(user_input)
        return LLMResponse(text="answer [1]")


@pytest.mark.asyncio
async def test_off_topic_question_below_relevance_cutoff_never_reaches_llm(captured_pool):
    llm = _FakeLLM()
    answer = await answer_question(
        None, FakeEmbeddingProvider(), llm, "parental leave policy?",
        reranker=FakeReranker({}), min_relevance=0.3,
    )
    assert llm.calls == []
    assert answer.answer == NO_CONTEXT_ANSWER


@pytest.mark.asyncio
async def test_relevant_chunks_pass_cutoff_and_reach_llm(captured_pool):
    llm = _FakeLLM()
    answer = await answer_question(
        None, FakeEmbeddingProvider(), llm, "q", top_k=2,
        reranker=FakeReranker({"d5.md": 0.8, "d7.md": 0.6}), min_relevance=0.3,
    )
    assert len(llm.calls) == 1
    assert [s.name for s in answer.sources] == ["d5.md", "d7.md"]


# --- CrossEncoderReranker against a stub model -----------------------------------

class _StubCrossEncoder:
    load_calls: list = []
    cached = True

    def __init__(self, model_name, local_files_only=False, **kwargs):
        _StubCrossEncoder.load_calls.append(local_files_only)
        _StubCrossEncoder.activation = kwargs.get("default_activation_function")
        if local_files_only and not _StubCrossEncoder.cached:
            raise OSError("not cached")
        self.thread = None

    def predict(self, pairs, **kwargs):
        self.thread = threading.current_thread()
        return np.array([len(passage) / 100 for _, passage in pairs], dtype=np.float32)


@pytest.fixture
def stub_cross_encoder(monkeypatch):
    _StubCrossEncoder.load_calls = []
    _StubCrossEncoder.cached = True
    module = types.ModuleType("sentence_transformers")
    module.CrossEncoder = _StubCrossEncoder
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    return _StubCrossEncoder


@pytest.mark.asyncio
async def test_cross_encoder_scores_pairs_off_loop_from_cache(stub_cross_encoder):
    reranker = CrossEncoderReranker("some/reranker")
    scores = await reranker.score("q", ["ab", "abcd"])

    assert scores == pytest.approx([0.02, 0.04])
    assert all(isinstance(s, float) for s in scores)
    assert stub_cross_encoder.load_calls == [True]
    assert reranker._get_model().thread is not threading.main_thread()
    # Sigmoid forced regardless of model default, so scores are always 0..1.
    assert type(stub_cross_encoder.activation).__name__ == "Sigmoid"


@pytest.mark.asyncio
async def test_cross_encoder_downloads_when_not_cached_and_skips_empty(stub_cross_encoder):
    stub_cross_encoder.cached = False
    reranker = CrossEncoderReranker("some/reranker")
    assert await reranker.score("q", []) == []
    assert stub_cross_encoder.load_calls == []  # empty input never loads the model
    await reranker.score("q", ["x"])
    assert stub_cross_encoder.load_calls == [True, False]


# --- CLI -------------------------------------------------------------------------

def test_cli_no_rerank_flag_and_relevance_display():
    args = build_parser().parse_args(["ask", "q", "--no-rerank"])
    assert args.no_rerank is True
    result = _result("Guide.md")
    result.rerank_score, result.similarity = 0.912, 0.7
    assert "[file; relevance 0.912, similarity 0.700]" in format_results([result])
