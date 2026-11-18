import uuid

import pytest
from sqlalchemy.dialects import postgresql

import app.retrieval.search as search_module
from app.chunking.chunker import Chunker
from app.cli import build_parser, describe_scope
from app.llm.base import LLMResponse
from app.project_numbers import extract_project_numbers, is_project_number
from app.rag.answer import NO_CONTEXT_ANSWER, answer_question, build_user_input, group_sources
from app.retrieval.search import ProjectScope, SearchFilters, SearchResult, _base_query, scoped_search
from tests.conftest import FakeEmbeddingProvider

DOC_ID = uuid.uuid4()


# --- extraction -----------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Project 10000001 overview", ["10000001"]),
    ("10000001pulsedashboardacr", ["10000001"]),          # glued to letters (one token for Postgres FTS)
    ("pulsedashboard-10000001-contapp", ["10000001"]),    # hyphenated (a negative int for Postgres FTS)
    ("10000001---pulse-dashboard---blueprint-138", ["10000001"]),
    ("123456789 and 1234567", []),                        # 9 and 7 digits: not project numbers
    ("ids 10035001, 10000001 and 10000001 again", ["10000001", "10035001"]),
    ("arabic-indic ١٠٠٠٠٠٠١", []),                         # only ASCII digits count
])
def test_extract_project_numbers(text, expected):
    assert extract_project_numbers(text) == expected


def test_extract_handles_none_and_multiple_texts():
    assert extract_project_numbers(None, "a 10000001", "", "b 10002003") == ["10000001", "10002003"]


def test_is_project_number():
    assert is_project_number("10000001")
    assert not is_project_number("1000000")
    assert not is_project_number("1000000a")


# --- chunker tagging --------------------------------------------------------------

def test_document_name_and_path_tag_every_chunk():
    text = "# Overview\nNothing numeric here.\n# Setup\nStill nothing."
    chunks = Chunker().chunk(text, DOC_ID, "h", {"name": "Guide.md", "path": "/02_Dev/10000001---pulse/Guide.md"})
    assert len(chunks) == 2
    assert all(c.project_numbers == ["10000001"] for c in chunks)


def test_chunk_text_and_heading_add_their_own_numbers():
    text = "# Migration from 10002003\nMoves data into 10035001pulseacr."
    chunks = Chunker().chunk(text, DOC_ID, "h", {"name": "10000001 - Plan", "path": None})
    assert chunks[0].project_numbers == ["10000001", "10002003", "10035001"]


def test_untagged_content_has_no_project_numbers():
    assert Chunker().chunk("Plain text.", DOC_ID, "h", {"name": "a.md"})[0].project_numbers == []


# --- search filter SQL -------------------------------------------------------------

def test_project_filter_uses_array_overlap():
    stmt = _base_query("m", SearchFilters(project_numbers=["10000001", "10002003"]))
    sql = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "chunks.project_numbers && ARRAY['10000001', '10002003']" in sql


# --- scoped_search decisions ----------------------------------------------------------

def _result(name, projects=()):
    return SearchResult(
        chunk_id=uuid.uuid4(), document_id=uuid.uuid4(), chunk_index=0, content="c", name=name,
        sharepoint_url=None, path=None, source_type="file", section=None, score=0.1,
        project_numbers=list(projects),
    )


@pytest.fixture
def fake_backend(monkeypatch):
    """search_chunks returns results only for the projects in `tagged`."""
    state = {"tagged": {"10000001"}, "calls": []}

    async def fake_search_chunks(session, provider, query, top_k, mode, filters, **kwargs):
        state["calls"].append(list(filters.project_numbers))
        if not filters.project_numbers:
            return [_result("general.md")]
        return [_result(f"{n}.md", [n]) for n in filters.project_numbers if n in state["tagged"]]

    async def fake_has_content(session, model_name, filters):
        return any(n in state["tagged"] for n in filters.project_numbers)

    monkeypatch.setattr(search_module, "search_chunks", fake_search_chunks)
    monkeypatch.setattr(search_module, "_project_has_content", fake_has_content)
    return state


@pytest.mark.asyncio
async def test_number_in_question_restricts_search(fake_backend):
    found = await scoped_search(None, FakeEmbeddingProvider(), "resources of project 10000001?")
    assert fake_backend["calls"] == [["10000001"]]
    assert found.scope == ProjectScope(["10000001"], explicit=False, fell_back=False)
    assert [r.name for r in found.results] == ["10000001.md"]


@pytest.mark.asyncio
async def test_unknown_number_falls_back_to_unrestricted(fake_backend):
    found = await scoped_search(None, FakeEmbeddingProvider(), "owner of 12345678?")
    assert fake_backend["calls"] == [[]]
    assert found.scope.fell_back is True
    assert [r.name for r in found.results] == ["general.md"]


@pytest.mark.asyncio
async def test_explicit_project_never_falls_back(fake_backend):
    found = await scoped_search(None, FakeEmbeddingProvider(), "owner?", filters=SearchFilters(project_numbers=["12345678"]))
    assert found.results == []
    assert found.scope == ProjectScope(["12345678"], explicit=True, fell_back=False)


@pytest.mark.asyncio
async def test_explicit_project_overrides_numbers_in_question(fake_backend):
    found = await scoped_search(
        None, FakeEmbeddingProvider(), "compare with 10002003", filters=SearchFilters(project_numbers=["10000001"])
    )
    assert fake_backend["calls"] == [["10000001"]]


@pytest.mark.asyncio
async def test_no_number_means_no_scope(fake_backend):
    found = await scoped_search(None, FakeEmbeddingProvider(), "how do I deploy?")
    assert found.scope is None
    assert fake_backend["calls"] == [[]]


# --- RAG -------------------------------------------------------------------------------

class _FakeLLM:
    def __init__(self):
        self.inputs = []

    async def generate(self, instructions, user_input):
        self.inputs.append(user_input)
        return LLMResponse(text="ok [1]")


@pytest.mark.asyncio
async def test_rag_tells_llm_about_restriction(fake_backend):
    llm = _FakeLLM()
    answer = await answer_question(None, FakeEmbeddingProvider(), llm, "resources of 10000001?")
    assert "restricted to content tagged with project number(s) 10000001" in llm.inputs[0]
    assert answer.project_scope.project_numbers == ["10000001"]


@pytest.mark.asyncio
async def test_rag_warns_llm_on_fallback(fake_backend):
    llm = _FakeLLM()
    await answer_question(None, FakeEmbeddingProvider(), llm, "owner of 12345678?")
    assert "no indexed content is tagged" in llm.inputs[0]
    assert "may concern other projects" in llm.inputs[0]


@pytest.mark.asyncio
async def test_rag_project_with_nothing_relevant_says_so_without_llm(fake_backend):
    llm = _FakeLLM()
    answer = await answer_question(
        None, FakeEmbeddingProvider(), llm, "q", filters=SearchFilters(project_numbers=["12345678"])
    )
    assert llm.inputs == []
    assert answer.answer != NO_CONTEXT_ANSWER
    assert "tagged with project 12345678" in answer.answer


def test_user_input_without_scope_has_no_note():
    text = build_user_input("Q?", group_sources([_result("a.md")]))
    assert text.startswith("Sources:")


# --- CLI -------------------------------------------------------------------------------

def test_cli_project_flag_validates_and_repeats():
    args = build_parser().parse_args(["search", "q", "--project", "10000001", "--project", " 10002003 "])
    assert args.project == ["10000001", "10002003"]
    with pytest.raises(SystemExit):
        build_parser().parse_args(["ask", "q", "--project", "1234"])


def test_describe_scope_messages():
    assert describe_scope(None) is None
    assert "detected in your question" in describe_scope(ProjectScope(["10000001"], explicit=False))
    assert "as requested" in describe_scope(ProjectScope(["10000001"], explicit=True))
    assert "showing results from all content" in describe_scope(ProjectScope(["12345678"], False, fell_back=True))
