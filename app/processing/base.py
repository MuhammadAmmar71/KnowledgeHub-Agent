"""
Common content-extraction interface. Spec section 29: every processor
implements the same shape, so the router (and everything upstream of it)
never needs to know which file type it's dealing with.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.sharepoint.files import DownloadedFile


@dataclass
class ExtractedContent:
    text: str
    # Structured extras per spec section 55 where applicable: page_number,
    # slide_number, sheet_name, heading, section, etc. Kept as a flat dict
    # rather than a fixed schema since which keys apply varies by file type.
    metadata: dict = field(default_factory=dict)
    # Non-fatal issues worth surfacing (OCR fallback used, a sheet got
    # truncated, a slide had no text) -- extraction still produced text,
    # but a human reviewing quality should see these.
    warnings: list[str] = field(default_factory=list)


class ContentProcessor(Protocol):
    async def extract(self, file: DownloadedFile) -> ExtractedContent: ...
