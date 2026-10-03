"""
Spec section 29: routes a downloaded file to the correct processor by
extension. Deliberately dumb -- no fallback guessing, no content
sniffing. An unsupported extension reaching this router is itself a bug
upstream (walk_drive/process_delta_item should never have classified it
as supported), so this raises loudly rather than silently returning
empty text.
"""
from __future__ import annotations

from app.processing.base import ContentProcessor, ExtractedContent
from app.processing.docx import DocxProcessor
from app.processing.drawio import DrawioProcessor
from app.processing.html import HtmlProcessor
from app.processing.image import ImageProcessor
from app.processing.json_processor import JsonProcessor
from app.processing.markdown import MarkdownProcessor
from app.processing.pdf import PdfProcessor
from app.processing.pptx import PptxProcessor
from app.processing.python import PythonProcessor
from app.processing.svg import SvgProcessor
from app.processing.text import TextProcessor
from app.processing.xlsx import XlsxProcessor
from app.sharepoint.files import DownloadedFile


class UnsupportedExtensionError(RuntimeError):
    pass


class ProcessorRouter:
    def __init__(self, processors: dict[str, ContentProcessor]):
        self._processors = processors

    async def extract(self, file: DownloadedFile, extension: str) -> ExtractedContent:
        processor = self._processors.get(extension.lower())
        if processor is None:
            raise UnsupportedExtensionError(f"No processor registered for extension '{extension}'")
        return await processor.extract(file)


def build_default_router() -> ProcessorRouter:
    # One shared ImageProcessor instance: used directly for .png, and
    # passed into PdfProcessor so a scanned/image-only PDF page can OCR
    # through the same Tesseract path rather than duplicating OCR logic.
    image_processor = ImageProcessor()
    return ProcessorRouter({
        ".md": MarkdownProcessor(),
        ".json": JsonProcessor(),
        ".py": PythonProcessor(),
        ".html": HtmlProcessor(),
        ".drawio": DrawioProcessor(),
        ".docx": DocxProcessor(),
        ".pptx": PptxProcessor(),
        ".xlsx": XlsxProcessor(),
        ".pdf": PdfProcessor(ocr_processor=image_processor),
        ".png": image_processor,
        ".txt": TextProcessor(),
        ".svg": SvgProcessor(),
    })
