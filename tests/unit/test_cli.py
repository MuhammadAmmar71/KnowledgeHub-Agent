import json
import uuid

from app.cli import _normalize_extension, build_parser, format_results, results_to_json
from app.retrieval.search import SearchResult


def _result(**overrides) -> SearchResult:
    values = dict(
        chunk_id=uuid.uuid4(), document_id=uuid.uuid4(), chunk_index=0,
        content="Open Docker Desktop first.\n\nThen   open PowerShell.",
        name="Pushing changes on Azure.md", sharepoint_url="https://x/Pushing.md",
        path="/02_Development/Pushing changes on Azure.md", source_type="file",
        section="Guide", score=0.032, similarity=0.83, semantic_rank=1, keyword_rank=2,
    )
    values.update(overrides)
    return SearchResult(**values)


def test_parser_defaults_and_repeatable_ext():
    args = build_parser().parse_args(["search", "docker", "--ext", "md", "--ext", ".TXT"])
    assert args.query == "docker"
    assert args.mode == "hybrid"
    assert args.top_k is None
    assert [_normalize_extension(e) for e in args.ext] == [".md", ".txt"]


def test_format_results_shows_citation_fields_and_flattened_snippet():
    out = format_results([_result()])
    assert out.startswith("1. Pushing changes on Azure.md › Guide  [file; similarity 0.830, keyword #2]")
    assert "url:  https://x/Pushing.md" in out
    assert "path: /02_Development/Pushing changes on Azure.md" in out
    assert "Open Docker Desktop first. Then open PowerShell." in out


def test_format_results_keyword_only_hit_and_page_without_path():
    out = format_results([_result(similarity=None, semantic_rank=None, path=None, section=None, source_type="page")])
    assert "[page; keyword #2]" in out
    assert "path:" not in out


def test_format_results_truncates_long_content_unless_full():
    long = "word " * 200
    assert "…" in format_results([_result(content=long)])
    assert "…" not in format_results([_result(content=long)], full=True)


def test_format_results_empty():
    assert format_results([]) == "No results."


def test_json_output_is_valid_and_complete():
    r = _result()
    data = json.loads(results_to_json([r]))
    assert data[0]["rank"] == 1
    assert data[0]["chunk_id"] == str(r.chunk_id)
    assert data[0]["sharepoint_url"] == "https://x/Pushing.md"
    assert data[0]["content"] == r.content
