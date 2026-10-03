"""
Spec section 30: pretty-print or convert into a readable textual
representation; avoid embedding huge unstructured minified JSON. Invalid
JSON is stored raw with a warning rather than failing the whole file --
per spec section 43, a malformed document is a permanent error to record,
not something that should crash the run.
"""
from __future__ import annotations

import json

from app.processing.base import ExtractedContent
from app.sharepoint.files import DownloadedFile


class JsonProcessor:
    async def extract(self, file: DownloadedFile) -> ExtractedContent:
        raw = file.content.decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return ExtractedContent(text=raw, warnings=[f"invalid JSON, stored raw text: {exc}"])

        text = json.dumps(parsed, indent=2, ensure_ascii=False, sort_keys=False)
        return ExtractedContent(text=text)
