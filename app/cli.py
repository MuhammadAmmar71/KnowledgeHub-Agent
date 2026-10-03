"""
Command-line interface.

    venv/bin/python -m app.cli search "how do I push changes to Azure"
    venv/bin/python -m app.cli search "release branches" -k 10 --ext .md --ext .docx
    venv/bin/python -m app.cli search "onboarding" --type page --mode semantic
    venv/bin/python -m app.cli search "docker login" --json
    venv/bin/python -m app.cli reprocess   # then POST /sync?mode=full and POST /sync/pages
    venv/bin/python -m app.cli ask "How do I deploy the Pulse dashboard to Azure?"

Talks to the database directly (no running server needed). The embedding
model loads in-process on each invocation, so expect a few seconds of
startup before results print.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import textwrap

from sqlalchemy import func, select, update

from app.config import get_settings
from app.db.database import build_engine, build_session_factory
from app.db.models import Chunk as ChunkModel
from app.db.models import Document, ProcessingStatus, SourceType
from app.embeddings.sentence_transformer import SentenceTransformerProvider
from app.llm.azure_openai import AzureOpenAIResponsesClient
from app.llm.base import LLMError
from app.rag.answer import RagAnswer, answer_question
from app.retrieval.search import SearchFilters, SearchMode, SearchResult, search_chunks

_WHITESPACE_RE = re.compile(r"\s+")


def _add_retrieval_args(parser: argparse.ArgumentParser, top_k_help: str) -> None:
    parser.add_argument("-k", "--top-k", type=int, default=None, help=top_k_help)
    parser.add_argument(
        "--mode", choices=[m.value for m in SearchMode], default=SearchMode.hybrid.value,
        help="hybrid = semantic + keyword (default); semantic = vectors only",
    )
    parser.add_argument("--type", choices=[t.value for t in SourceType], help="only files or only pages")
    parser.add_argument(
        "--ext", action="append", default=[], metavar=".EXT", help="only these file extensions (repeatable)"
    )
    parser.add_argument("--path", help="only documents whose path contains this text")
    parser.add_argument("--json", action="store_true", help="machine-readable output")


def _filters_from_args(args: argparse.Namespace) -> SearchFilters:
    return SearchFilters(
        source_type=SourceType(args.type) if args.type else None,
        extensions=[_normalize_extension(e) for e in args.ext],
        path_contains=args.path,
    )


def _build_embedding_provider(settings) -> SentenceTransformerProvider:
    return SentenceTransformerProvider(
        settings.embedding_model_name,
        device=settings.embedding_device,
        batch_size=settings.embedding_batch_size,
        query_instruction=settings.embedding_query_instruction,
    )


async def _check_index_ready(session, model_name: str) -> bool:
    # An empty index for the configured model almost always means the
    # embedding stage hasn't run (or the model was changed) -- say so
    # instead of printing a puzzling "No results."
    embedded = await session.scalar(
        select(func.count()).select_from(ChunkModel).where(ChunkModel.embedding_model == model_name)
    )
    if not embedded:
        print(
            f"No chunks are embedded with '{model_name}'. Run a sync or POST /embeddings/run first.",
            file=sys.stderr,
        )
    return bool(embedded)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="Auximo Knowledge Agent CLI")
    commands = parser.add_subparsers(dest="command", required=True)

    search = commands.add_parser("search", help="search indexed SharePoint content")
    search.add_argument("query", help="free-text question or keywords")
    _add_retrieval_args(search, top_k_help="number of results (default from settings)")
    search.add_argument("--full", action="store_true", help="print whole chunks instead of snippets")

    ask = commands.add_parser("ask", help="answer a question from SharePoint content using the LLM")
    ask.add_argument("question", help="the question to answer")
    _add_retrieval_args(ask, top_k_help="chunks retrieved as context (default from settings)")
    ask.add_argument("--show-context", action="store_true", help="also print the excerpts sent to the LLM")

    commands.add_parser(
        "reprocess",
        help="mark every processed document PENDING so the next full sync re-extracts and re-chunks it "
             "(use after changing extraction or chunking logic)",
    )
    return parser


def _normalize_extension(value: str) -> str:
    value = value.strip().lower()
    return value if value.startswith(".") else f".{value}"


def _snippet(content: str, width: int = 300) -> str:
    flat = _WHITESPACE_RE.sub(" ", content).strip()
    return flat if len(flat) <= width else flat[: width - 1].rstrip() + "…"


def format_results(results: list[SearchResult], full: bool = False) -> str:
    if not results:
        return "No results."
    blocks = []
    for rank, r in enumerate(results, start=1):
        title = r.name + (f" › {r.section}" if r.section else "")
        signals = []
        if r.similarity is not None:
            signals.append(f"similarity {r.similarity:.3f}")
        if r.keyword_rank is not None:
            signals.append(f"keyword #{r.keyword_rank}")
        lines = [f"{rank}. {title}  [{r.source_type}; {', '.join(signals) or 'n/a'}]"]
        if r.path:
            lines.append(f"   path: {r.path}")
        if r.sharepoint_url:
            lines.append(f"   url:  {r.sharepoint_url}")
        body = r.content.strip() if full else _snippet(r.content)
        lines.append(textwrap.indent(textwrap.fill(body, width=100) if not full else body, "   "))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def results_to_json(results: list[SearchResult]) -> str:
    return json.dumps(
        [
            {
                "rank": rank,
                "score": r.score,
                "similarity": r.similarity,
                "semantic_rank": r.semantic_rank,
                "keyword_rank": r.keyword_rank,
                "name": r.name,
                "section": r.section,
                "source_type": r.source_type,
                "path": r.path,
                "sharepoint_url": r.sharepoint_url,
                "chunk_index": r.chunk_index,
                "chunk_id": str(r.chunk_id),
                "document_id": str(r.document_id),
                "content": r.content,
            }
            for rank, r in enumerate(results, start=1)
        ],
        indent=2,
        ensure_ascii=False,
    )


async def _run_search(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    provider = _build_embedding_provider(settings)
    try:
        async with session_factory() as session:
            if not await _check_index_ready(session, provider.model_name):
                return 1
            results = await search_chunks(
                session, provider, args.query,
                top_k=args.top_k or settings.search_default_top_k,
                mode=SearchMode(args.mode),
                filters=_filters_from_args(args),
            )
    finally:
        await engine.dispose()

    print(results_to_json(results) if args.json else format_results(results, full=args.full))
    return 0


def format_answer(result: RagAnswer, show_context: bool = False) -> str:
    lines = [result.answer.strip()]
    if result.incomplete_reason:
        lines.append(f"\n[answer was cut off: {result.incomplete_reason}]")
    if result.sources:
        cited = set(result.cited_numbers)
        # Cited sources first (in citation order), then anything retrieved
        # but not cited, so the reader sees what the answer rests on.
        ordered = result.cited_sources + [s for s in result.sources if s.number not in cited]
        lines.append("\nSources:")
        for source in ordered:
            marker = "" if source.number in cited else "  (retrieved, not cited)"
            sections = f" › {', '.join(source.sections)}" if source.sections else ""
            lines.append(f"  [{source.number}] {source.name}{sections}{marker}")
            if source.sharepoint_url:
                lines.append(f"      {source.sharepoint_url}")
    if show_context and result.sources:
        lines.append("\nContext sent to the LLM:")
        for source in result.sources:
            lines.append(f"\n[{source.number}] {source.name}")
            lines.extend(textwrap.indent(excerpt, "    ") for excerpt in source.excerpts)
    return "\n".join(lines)


def answer_to_json(result: RagAnswer) -> str:
    return json.dumps(
        {
            "question": result.question,
            "answer": result.answer,
            "incomplete_reason": result.incomplete_reason,
            "llm_called": result.llm_called,
            "cited": result.cited_numbers,
            "sources": [
                {
                    "number": s.number,
                    "name": s.name,
                    "sections": s.sections,
                    "source_type": s.source_type,
                    "path": s.path,
                    "sharepoint_url": s.sharepoint_url,
                    "document_id": str(s.document_id),
                    "cited": s.number in result.cited_numbers,
                }
                for s in result.sources
            ],
        },
        indent=2,
        ensure_ascii=False,
    )


async def _run_ask(args: argparse.Namespace) -> int:
    settings = get_settings()
    api_key = settings.azure_openai_api_key.get_secret_value() if settings.azure_openai_api_key else ""
    try:
        llm = AzureOpenAIResponsesClient(
            endpoint=settings.azure_openai_endpoint,
            api_key=api_key,
            deployment=settings.azure_openai_deployment,
            temperature=settings.llm_temperature,
            max_output_tokens=settings.llm_max_output_tokens,
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
        )
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    provider = _build_embedding_provider(settings)
    try:
        async with session_factory() as session:
            if not await _check_index_ready(session, provider.model_name):
                return 1
            result = await answer_question(
                session, provider, llm, args.question,
                top_k=args.top_k or settings.rag_top_k,
                mode=SearchMode(args.mode),
                filters=_filters_from_args(args),
            )
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await llm.aclose()
        await engine.dispose()

    print(answer_to_json(result) if args.json else format_answer(result, show_context=args.show_context))
    return 0


# Documents that finished (or got past) chunking. Failed/pending ones are
# already retried by sync, so they're left as they are.
_REPROCESSABLE_STATUSES = {
    ProcessingStatus.chunked,
    ProcessingStatus.embedding,
    ProcessingStatus.indexed,
    ProcessingStatus.embedding_failed,
}


async def _run_reprocess() -> int:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        async with build_session_factory(engine)() as session:
            result = await session.execute(
                update(Document)
                .where(Document.is_deleted.is_(False), Document.processing_status.in_(_REPROCESSABLE_STATUSES))
                .values(processing_status=ProcessingStatus.pending, processing_error=None)
            )
            await session.commit()
    finally:
        await engine.dispose()
    print(
        f"Marked {result.rowcount} documents PENDING. Existing chunks stay searchable until replaced.\n"
        "Next: POST /sync?mode=full and POST /sync/pages (embedding runs automatically afterwards)."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Keep stdout clean (especially for --json): library chatter goes to
    # stderr at WARNING+ only.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    if args.command == "search":
        return asyncio.run(_run_search(args))
    if args.command == "ask":
        return asyncio.run(_run_ask(args))
    if args.command == "reprocess":
        return asyncio.run(_run_reprocess())
    return 2


if __name__ == "__main__":
    sys.exit(main())
