"""
Markdown files are already structured, clean text -- spec section 30 says
preserve headings/lists/code blocks/tables, which a byte-level decode does
by construction. Any "cleaning" beyond decoding risks destroying the exact
structure that's worth preserving, so this stays deliberately minimal.
"""
from __future__ import annotations

from app.processing.base import ExtractedContent
from app.sharepoint.files import DownloadedFile


class MarkdownProcessor:
    async def extract(self, file: DownloadedFile) -> ExtractedContent:
        text = file.content.decode("utf-8", errors="replace")
        return ExtractedContent(text=text)
