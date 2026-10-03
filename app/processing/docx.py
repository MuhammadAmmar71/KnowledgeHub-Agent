"""
Spec section 30: extract paragraphs, headings, tables; preserve document
structure in normalized text.
"""
from __future__ import annotations

import io

from docx import Document as DocxDocument

from app.processing.base import ExtractedContent
from app.sharepoint.files import DownloadedFile


class DocxProcessor:
    async def extract(self, file: DownloadedFile) -> ExtractedContent:
        try:
            document = DocxDocument(io.BytesIO(file.content))
        except Exception as exc:
            return ExtractedContent(text="", warnings=[f"failed to open .docx: {exc}"])

        parts: list[str] = []

        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            style_name = (paragraph.style.name if paragraph.style else "") or ""
            if style_name.lower().startswith("heading"):
                parts.append(f"\n## {text}")
            else:
                parts.append(text)

        for table in document.tables:
            parts.append("\n[TABLE]")
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                parts.append(" | ".join(cells))
            parts.append("[/TABLE]")

        return ExtractedContent(text="\n".join(parts))
