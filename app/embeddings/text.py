"""
What actually gets embedded for a chunk.

The chunker lifts section markers (headings, page/slide numbers, sheet
names) out of chunk content into metadata. That keeps stored content clean
for display and citations, but a chunk embedded on its own text alone loses
the context of which document and section it came from. This puts that
context back -- only in the text sent to the model; stored content is
never modified.
"""
from __future__ import annotations


def _section_label(metadata: dict) -> str | None:
    if metadata.get("heading"):
        return str(metadata["heading"])
    if metadata.get("page_number") is not None:
        return f"Page {metadata['page_number']}"
    if metadata.get("slide_number") is not None:
        return f"Slide {metadata['slide_number']}"
    if metadata.get("sheet_name"):
        return f"Sheet {metadata['sheet_name']}"
    return None


def build_embedding_text(content: str, metadata: dict) -> str:
    context = [part for part in (metadata.get("name"), _section_label(metadata)) if part]
    if not context:
        return content
    return f"{' > '.join(context)}\n{content}"
