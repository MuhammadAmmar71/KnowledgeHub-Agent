"""
Structure-aware chunking.

Spec section 33's five-point strategy, applied in order:
1. split by document sections/headings
2. preserve semantic units (paragraph/sentence boundaries preferred over
   a hard character cut)
3. split oversized sections
4. apply modest overlap
5. keep source metadata on every chunk

Spec section 34: chunk identity is deterministic from
(document identity + content hash + chunk index) -- never a random UUID.
Here that's implemented as a uuid5 derived from exactly that triple, so
the chunk's own primary key IS its identity check: reprocessing the same
content always regenerates the same chunk IDs, and a genuine content
change (new hash) always produces entirely new IDs, which is exactly what
makes the transactional "delete old chunks, insert new ones" replacement
in spec section 57 safe and simple.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

# Fixed, arbitrary namespace UUID -- only needs to be stable across runs,
# never regenerate this value.
_CHUNK_ID_NAMESPACE = uuid.UUID("6f1c1b2e-3b8a-4b8b-9c3a-8f0f6e6f1a11")

DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 120

# One combined regex recognizes every structural marker the Phase 5
# processors actually emit, in document order, regardless of which file
# type produced them:
#   [PAGE 3]                  -- PdfProcessor
#   --- SLIDE 2 ---           -- PptxProcessor
#   SHEET: Sheet1             -- XlsxProcessor
#   ## Heading / # Heading    -- MarkdownProcessor (native) and
#                                 DocxProcessor's "## " heading convention
_SECTION_MARKER_RE = re.compile(
    r"^\[PAGE (?P<page_num>\d+)\]\s*$"
    r"|^--- SLIDE (?P<slide_num>\d+) ---\s*$"
    r"|^SHEET: (?P<sheet_name>.+)$"
    r"|^#{1,6}\s+(?P<heading_text>.+)$",
    re.MULTILINE,
)


@dataclass
class Chunk:
    id: uuid.UUID
    chunk_index: int
    content: str
    token_count: int
    metadata: dict = field(default_factory=dict)


def _section_metadata_from_match(match: re.Match) -> dict:
    if match.group("page_num") is not None:
        return {"page_number": int(match.group("page_num"))}
    if match.group("slide_num") is not None:
        return {"slide_number": int(match.group("slide_num"))}
    if match.group("sheet_name") is not None:
        return {"sheet_name": match.group("sheet_name").strip()}
    if match.group("heading_text") is not None:
        return {"heading": match.group("heading_text").strip()}
    return {}


def _split_into_sections(text: str) -> list[tuple[dict, str]]:
    """
    Returns (section_metadata, section_text) pairs in document order. Text
    with no recognized markers at all (plain .py, .json, .html, .drawio,
    or a normalized SharePoint page) comes back as a single section with
    no extra metadata -- that's correct, not a failure to detect structure.
    """
    matches = list(_SECTION_MARKER_RE.finditer(text))
    if not matches:
        return [({}, text)] if text.strip() else []

    sections: list[tuple[dict, str]] = []

    preamble = text[: matches[0].start()].strip()
    if preamble:
        sections.append(({}, preamble))

    for i, match in enumerate(matches):
        section_meta = _section_metadata_from_match(match)
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section_text = text[start:end].strip()
        if section_text:
            sections.append((section_meta, section_text))

    return sections


def _split_by_sentences(text: str, chunk_size: int) -> list[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    pieces: list[str] = []
    current = ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) <= chunk_size:
            current = candidate
        else:
            if current:
                pieces.append(current)
            if len(sentence) <= chunk_size:
                current = sentence
            else:
                # Last resort: a single "sentence" longer than chunk_size
                # (e.g. no punctuation at all) gets a hard character cut.
                for start in range(0, len(sentence), chunk_size):
                    pieces.append(sentence[start:start + chunk_size])
                current = ""
    if current:
        pieces.append(current)
    return pieces


def _split_oversized(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    if len(text) <= chunk_size:
        return [text]

    paragraphs = re.split(r"\n{2,}", text)
    pieces: list[str] = []
    current = ""

    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= chunk_size:
            current = candidate
            continue

        if current:
            pieces.append(current)

        if len(paragraph) <= chunk_size:
            current = paragraph
        else:
            pieces.extend(_split_by_sentences(paragraph, chunk_size))
            current = ""

    if current:
        pieces.append(current)

    if chunk_overlap > 0 and len(pieces) > 1:
        overlapped = [pieces[0]]
        for i in range(1, len(pieces)):
            prev_tail = pieces[i - 1][-chunk_overlap:]
            overlapped.append(f"{prev_tail}{pieces[i]}")
        return overlapped

    return pieces


def _estimate_token_count(text: str) -> int:
    """
    Rough heuristic (~4 characters/token for English text) -- deliberately
    not tied to any specific tokenizer yet, since no embedding model has
    been chosen (that's Phase 7). Good enough for capacity planning today;
    swapping in a real tokenizer later changes this one function, not the
    chunk shape or IDs.
    """
    return max(1, len(text) // 4)


def _deterministic_chunk_id(document_id: uuid.UUID, content_hash: str, chunk_index: int) -> uuid.UUID:
    name = f"{document_id}:{content_hash}:{chunk_index}"
    return uuid.uuid5(_CHUNK_ID_NAMESPACE, name)


class Chunker:
    def __init__(self, chunk_size: int = DEFAULT_CHUNK_SIZE, chunk_overlap: int = DEFAULT_CHUNK_OVERLAP):
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap

    def chunk(
        self,
        text: str,
        document_id: uuid.UUID,
        content_hash: str,
        base_metadata: dict | None = None,
    ) -> list[Chunk]:
        """
        base_metadata (spec section 55) -- source_type, sharepoint_item_id,
        name, path, sharepoint_url -- is merged into every chunk so each
        one can be traced back to its source independent of the others.
        """
        base_metadata = base_metadata or {}
        sections = _split_into_sections(text)

        chunks: list[Chunk] = []
        index = 0
        for section_meta, section_text in sections:
            for piece in _split_oversized(section_text, self._chunk_size, self._chunk_overlap):
                metadata = {**base_metadata, **section_meta, "chunk_index": index}
                chunks.append(Chunk(
                    id=_deterministic_chunk_id(document_id, content_hash, index),
                    chunk_index=index,
                    content=piece,
                    token_count=_estimate_token_count(piece),
                    metadata=metadata,
                ))
                index += 1

        return chunks
