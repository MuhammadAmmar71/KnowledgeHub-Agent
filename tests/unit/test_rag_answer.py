import uuid

import pytest

from app.cli import format_answer
from app.llm.base import LLMResponse
from app.rag.answer import (
    NO_CONTEXT_ANSWER,
    SYSTEM_INSTRUCTIONS,
    answer_question,
    build_user_input,
    extract_citations,
    group_sources,
)
from app.retrieval.search import SearchResult
from tests.conftest import FakeEmbeddingProvider

DOC_A, DOC_B = uuid.uuid4(), uuid.uuid4()


def _result(document_id, name, content, section=None, url="https://sp/x"):
    return SearchResult(
        chunk_id=uuid.uuid4(), document_id=document_id, chunk_index=0, content=content, name=name,
        sharepoint_url=url, path=f"/{name}", source_type="file", section=section, score=0.1,
    )


class FakeLLM:
    def __init__(self, text="Use docker push [1]."):
        self.text = text
        self.calls = []

    async def generate(self, instructions, user_input):
        self.calls.append((instructions, user_input))
        return LLMResponse(text=self.text)


def _search_returning(results):
    async def search(session, provider, query, top_k, mode, filters):
        return results
    return search


def test_group_sources_one_number_per_document_in_rank_order():
    sources = group_sources([
        _result(DOC_A, "Guide.md", "step one", section="Guide"),
        _result(DOC_B, "Perms.md", "contributor role"),
        _result(DOC_A, "Guide.md", "step two", section="Required Permissions"),
    ])
    assert [(s.number, s.name) for s in sources] == [(1, "Guide.md"), (2, "Perms.md")]
    assert sources[0].sections == ["Guide", "Required Permissions"]
    assert sources[0].excerpts == ["step one", "step two"]


def test_user_input_numbers_sources_and_ends_with_question():
    text = build_user_input("How?", group_sources([_result(DOC_A, "Guide.md", "step one", section="Guide")]))
    assert "[1] Guide.md (sections: Guide)" in text
    assert "step one" in text
    assert text.rstrip().endswith("Question: How?")


def test_extract_citations_ignores_unknown_numbers_and_dedupes():
    assert extract_citations("A [2]. B [1][2]. C [7].", {1, 2}) == [2, 1]


def test_system_prompt_covers_spec_38_rules():
    lowered = SYSTEM_INSTRUCTIONS.lower()
    for phrase in (
        "only from the provided excerpts", "do not invent", "cite", "missing", "authoritative",
        "directly supported by an excerpt", "do not add your own characterisations",
    ):
        assert phrase in lowered


@pytest.mark.asyncio
async def test_answer_question_calls_llm_with_context_and_reports_citations():
    llm = FakeLLM("Push the image [1]. Needs Contributor [2].")
    results = [_result(DOC_A, "Guide.md", "docker push ..."), _result(DOC_B, "Perms.md", "Contributor")]

    answer = await answer_question(
        None, FakeEmbeddingProvider(), llm, "How do I deploy?", search=_search_returning(results)
    )

    instructions, user_input = llm.calls[0]
    assert instructions == SYSTEM_INSTRUCTIONS
    assert "docker push" in user_input and "How do I deploy?" in user_input
    assert answer.cited_numbers == [1, 2]
    assert [s.name for s in answer.cited_sources] == ["Guide.md", "Perms.md"]


@pytest.mark.asyncio
async def test_no_retrieval_results_means_no_llm_call():
    llm = FakeLLM()
    answer = await answer_question(None, FakeEmbeddingProvider(), llm, "Unknown?", search=_search_returning([]))
    assert llm.calls == []
    assert answer.answer == NO_CONTEXT_ANSWER
    assert answer.llm_called is False


@pytest.mark.asyncio
async def test_format_answer_lists_cited_first_then_uncited():
    results = [_result(DOC_A, "Guide.md", "a", url="https://sp/a"), _result(DOC_B, "Perms.md", "b", url="https://sp/b")]
    answer = await answer_question(
        None, FakeEmbeddingProvider(), FakeLLM("Only perms matter [2]."), "Q?", search=_search_returning(results)
    )
    out = format_answer(answer)
    assert out.index("[2] Perms.md") < out.index("[1] Guide.md")
    assert "[1] Guide.md  (retrieved, not cited)" in out
    assert "https://sp/b" in out
