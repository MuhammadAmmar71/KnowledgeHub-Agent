"""
RAG answer generation (spec sections 37-39): retrieve -> build numbered
source context -> ask the LLM -> return the answer with citations.

Context is grouped by document, so a citation number refers to a source
document (what a reader can open), not to an individual chunk. Every
source carries its SharePoint webUrl (spec section 39).

When retrieval finds nothing, the LLM is not called at all: there is
nothing to ground an answer in, and asking anyway invites an invented one.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.embeddings.base import EmbeddingProvider
from app.llm.base import LLMProvider
from app.retrieval.search import SearchFilters, SearchMode, SearchResult, search_chunks

# Spec section 38. Explicitly frames the retrieved text as excerpts and
# SharePoint as the authoritative source, never the index itself.
SYSTEM_INSTRUCTIONS = """\
You are the Auximo Knowledge Agent. You answer questions using excerpts retrieved from \
Auximo's SharePoint knowledge hub.

Rules:
- Answer only from the provided excerpts. Do not use outside knowledge to fill gaps, and do not invent \
names, numbers, steps, links, or policies.
- Every statement must be directly supported by an excerpt. Do not add your own characterisations, \
benefits, purposes, recommendations, or concluding remarks (e.g. "this improves productivity") unless an \
excerpt says so. Stay close to the excerpts' own wording.
- Cite the sources you used with their bracketed numbers, e.g. [1] or [2][3], right after the \
statements they support.
- If the excerpts do not contain the answer, say so plainly. If they only partially answer the \
question, answer what they support and state clearly what is missing.
- If excerpts conflict or are ambiguous, point that out instead of picking one silently.
- The excerpts are fragments of documents and may be incomplete or out of date; SharePoint itself is \
the authoritative source. Do not claim anything about SharePoint content beyond what the excerpts show.
- Be concise and practical. Use short steps or bullet points for procedures.
- Answer in the same language as the question."""

NO_CONTEXT_ANSWER = (
    "I couldn't find anything in the indexed SharePoint content that relates to this question. "
    "Try rephrasing it, or widen any filters you used."
)

_CITATION_RE = re.compile(r"\[(\d+)\]")


@dataclass
class Source:
    number: int
    document_id: uuid.UUID
    name: str
    sharepoint_url: str | None
    path: str | None
    source_type: str
    sections: list[str] = field(default_factory=list)
    excerpts: list[str] = field(default_factory=list)


@dataclass
class RagAnswer:
    question: str
    answer: str
    sources: list[Source]
    cited_numbers: list[int]
    incomplete_reason: str | None = None
    llm_called: bool = True

    @property
    def cited_sources(self) -> list[Source]:
        cited = set(self.cited_numbers)
        return [s for s in self.sources if s.number in cited]


def group_sources(results: list[SearchResult]) -> list[Source]:
    """One Source per document, numbered by the rank of its best chunk."""
    by_document: dict[uuid.UUID, Source] = {}
    for result in results:
        source = by_document.get(result.document_id)
        if source is None:
            source = Source(
                number=len(by_document) + 1,
                document_id=result.document_id,
                name=result.name,
                sharepoint_url=result.sharepoint_url,
                path=result.path,
                source_type=result.source_type,
            )
            by_document[result.document_id] = source
        if result.section and result.section not in source.sections:
            source.sections.append(result.section)
        source.excerpts.append(result.content.strip())
    return list(by_document.values())


def build_context(sources: list[Source]) -> str:
    blocks = []
    for source in sources:
        header = f"[{source.number}] {source.name}"
        if source.sections:
            header += f" (sections: {', '.join(source.sections)})"
        lines = [header]
        if source.path:
            lines.append(f"Path: {source.path}")
        lines.append("Excerpts:")
        lines.extend(f"---\n{excerpt}" for excerpt in source.excerpts)
        blocks.append("\n".join(lines))
    return "\n\n=====\n\n".join(blocks)


def build_user_input(question: str, sources: list[Source]) -> str:
    return f"Sources:\n\n{build_context(sources)}\n\n=====\n\nQuestion: {question}"


def extract_citations(answer: str, valid_numbers: set[int]) -> list[int]:
    """Cited source numbers in first-mention order, ignoring any the model made up."""
    seen: list[int] = []
    for match in _CITATION_RE.finditer(answer):
        number = int(match.group(1))
        if number in valid_numbers and number not in seen:
            seen.append(number)
    return seen


SearchFn = Callable[..., Awaitable[list[SearchResult]]]


async def answer_question(
    session: AsyncSession,
    embedding_provider: EmbeddingProvider,
    llm: LLMProvider,
    question: str,
    top_k: int = 8,
    mode: SearchMode = SearchMode.hybrid,
    filters: SearchFilters | None = None,
    search: SearchFn = search_chunks,
) -> RagAnswer:
    results = await search(session, embedding_provider, question, top_k=top_k, mode=mode, filters=filters)
    sources = group_sources(results)

    if not sources:
        return RagAnswer(question=question, answer=NO_CONTEXT_ANSWER, sources=[], cited_numbers=[], llm_called=False)

    response = await llm.generate(SYSTEM_INSTRUCTIONS, build_user_input(question, sources))
    return RagAnswer(
        question=question,
        answer=response.text,
        sources=sources,
        cited_numbers=extract_citations(response.text, {s.number for s in sources}),
        incomplete_reason=response.incomplete_reason,
    )
