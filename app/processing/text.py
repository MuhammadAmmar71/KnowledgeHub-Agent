"""
Plain .txt files: no structure to parse, so a byte-level decode is the
whole extraction. Any markdown-style "#" headings a .txt happens to contain
are still picked up by the chunker's section splitting.
"""
from __future__ import annotations

from app.processing.base import ExtractedContent
from app.sharepoint.files import DownloadedFile


class TextProcessor:
    async def extract(self, file: DownloadedFile) -> ExtractedContent:
        # utf-8-sig strips a leading BOM, which Windows-authored .txt files
        # commonly carry and which would otherwise leak into the first chunk.
        text = file.content.decode("utf-8-sig", errors="replace")
        return ExtractedContent(text=text)
