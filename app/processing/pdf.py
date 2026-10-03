"""
Spec section 30: extract text from pages, store page numbers, use OCR
only where text extraction is insufficient -- not for every page
unconditionally (that would be slow and pointless for text-layer PDFs).
"""
from __future__ import annotations

import io
from typing import Callable

from pypdf import PdfReader

from app.processing.base import ExtractedContent
from app.processing.image import ImageProcessor
from app.sharepoint.files import DownloadedFile

_DEFAULT_MIN_CHARS_FOR_TEXT_LAYER = 20


class PdfProcessor:
    def __init__(
        self,
        ocr_processor: ImageProcessor | None = None,
        min_chars_per_page: int = _DEFAULT_MIN_CHARS_FOR_TEXT_LAYER,
        reader_factory: Callable[[io.BytesIO], PdfReader] | None = None,
    ):
        self._ocr = ocr_processor
        self._min_chars = min_chars_per_page
        # Injectable for testing -- pypdf can read existing PDFs easily but
        # authoring a real text-bearing PDF in a test needs a library this
        # project doesn't otherwise depend on, so tests inject a fake
        # reader instead of constructing real PDF bytes.
        self._reader_factory = reader_factory or PdfReader

    async def extract(self, file: DownloadedFile) -> ExtractedContent:
        warnings: list[str] = []
        try:
            reader = self._reader_factory(io.BytesIO(file.content))
        except Exception as exc:
            return ExtractedContent(text="", warnings=[f"failed to open PDF: {exc}"])

        page_texts: dict[int, str] = {}
        pages_needing_ocr: list[int] = []
        pages = list(reader.pages)

        for idx, page in enumerate(pages, start=1):
            try:
                text = (page.extract_text() or "").strip()
            except Exception as exc:
                text = ""
                warnings.append(f"page {idx}: text extraction failed ({exc})")

            if len(text) < self._min_chars:
                pages_needing_ocr.append(idx)
            else:
                page_texts[idx] = text

        if pages_needing_ocr:
            if self._ocr is not None:
                try:
                    ocr_results = await self._ocr.ocr_pdf_pages(file.content, pages_needing_ocr)
                    for idx in pages_needing_ocr:
                        page_texts[idx] = ocr_results.get(idx, "").strip() or "(no text detected by OCR)"
                    warnings.append(f"OCR fallback used for page(s): {pages_needing_ocr}")
                except Exception as exc:
                    warnings.append(f"OCR fallback failed: {exc}")
                    for idx in pages_needing_ocr:
                        page_texts.setdefault(idx, "(OCR unavailable)")
            else:
                warnings.append(
                    f"{len(pages_needing_ocr)} page(s) had little/no extractable text "
                    "and no OCR processor was configured"
                )
                for idx in pages_needing_ocr:
                    page_texts.setdefault(idx, "(no text layer, OCR not configured)")

        parts = [f"[PAGE {idx}]\n{page_texts.get(idx, '')}" for idx in range(1, len(pages) + 1)]
        text = "\n\n".join(parts)
        return ExtractedContent(text=text, warnings=warnings, metadata={"page_count": len(pages)})
