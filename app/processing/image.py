"""
Spec section 30: OCR/vision extraction for PNGs, store image metadata.

Uses local Tesseract (via pytesseract) rather than a cloud vision API --
this machine's tesseract-ocr + poppler-utils system packages must be
installed for this to work (see README). pytesseract, PIL's image
decode, and pdf2image's poppler subprocess call are all
blocking/CPU-bound, so every call here runs via asyncio.to_thread rather
than directly in the event loop (same reasoning as the MSAL auth fix in
Phase 1/2: a slow OCR call must not freeze unrelated in-flight requests).
"""
from __future__ import annotations

import asyncio
import io
import logging

import pytesseract
from PIL import Image
from pdf2image import convert_from_bytes

from app.processing.base import ExtractedContent
from app.sharepoint.files import DownloadedFile

logger = logging.getLogger("knowledge_agent.processing.image")


class ImageProcessor:
    async def extract(self, file: DownloadedFile) -> ExtractedContent:
        try:
            text, width, height = await asyncio.to_thread(self._ocr_image_sync, file.content)
        except Exception as exc:
            return ExtractedContent(text="", warnings=[f"OCR failed: {exc}"])
        return ExtractedContent(text=text.strip(), metadata={"width": width, "height": height})

    @staticmethod
    def _ocr_image_sync(content: bytes) -> tuple[str, int, int]:
        image = Image.open(io.BytesIO(content))
        text = pytesseract.image_to_string(image)
        return text, image.width, image.height

    async def ocr_pdf_pages(self, pdf_bytes: bytes, page_numbers: list[int]) -> dict[int, str]:
        """
        Rasterizes and OCRs only the specific pages that actually need it
        (no usable text layer) -- called by PdfProcessor as a per-page
        fallback, not for every page unconditionally.
        """
        results: dict[int, str] = {}
        for page_num in page_numbers:
            try:
                text = await asyncio.to_thread(self._ocr_pdf_page_sync, pdf_bytes, page_num)
                results[page_num] = text
            except Exception as exc:
                logger.warning("pdf_page_ocr_failed page=%d error=%s", page_num, exc)
        return results

    @staticmethod
    def _ocr_pdf_page_sync(pdf_bytes: bytes, page_num: int) -> str:
        images = convert_from_bytes(pdf_bytes, first_page=page_num, last_page=page_num)
        if not images:
            return ""
        return pytesseract.image_to_string(images[0])
