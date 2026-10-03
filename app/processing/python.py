"""
Spec section 30: preserve code exactly where possible, don't blindly split
mid-function (that's a chunking concern for Phase 6, not extraction).
"""
from __future__ import annotations

from app.processing.base import ExtractedContent
from app.sharepoint.files import DownloadedFile


class PythonProcessor:
    async def extract(self, file: DownloadedFile) -> ExtractedContent:
        text = file.content.decode("utf-8", errors="replace")
        return ExtractedContent(text=text)
