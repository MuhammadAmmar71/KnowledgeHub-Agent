import pytest

from app.processing.image import ImageProcessor
from app.processing.pdf import PdfProcessor
from app.processing.router import ProcessorRouter, UnsupportedExtensionError, build_default_router
from app.sharepoint.files import DownloadedFile


def _file(name: str, content: bytes = b"") -> DownloadedFile:
    return DownloadedFile(item_id="1", filename=name, content=content, content_type=None)


@pytest.mark.asyncio
async def test_router_dispatches_to_registered_processor():
    class _StubProcessor:
        async def extract(self, file):
            from app.processing.base import ExtractedContent
            return ExtractedContent(text=f"handled: {file.filename}")

    router = ProcessorRouter({".md": _StubProcessor()})
    result = await router.extract(_file("t.md"), ".md")
    assert result.text == "handled: t.md"


@pytest.mark.asyncio
async def test_router_raises_for_unregistered_extension():
    router = ProcessorRouter({".md": None})
    with pytest.raises(UnsupportedExtensionError):
        await router.extract(_file("t.exe"), ".exe")


@pytest.mark.asyncio
async def test_router_extension_match_is_case_insensitive():
    class _StubProcessor:
        async def extract(self, file):
            from app.processing.base import ExtractedContent
            return ExtractedContent(text="ok")

    router = ProcessorRouter({".md": _StubProcessor()})
    result = await router.extract(_file("T.MD"), ".MD")
    assert result.text == "ok"


def test_default_router_registers_all_supported_extensions():
    router = build_default_router()
    expected = {".md", ".json", ".py", ".html", ".drawio", ".docx", ".pptx", ".xlsx", ".pdf", ".png", ".txt", ".svg"}
    assert set(router._processors.keys()) == expected


def test_default_router_shares_one_image_processor_between_png_and_pdf_ocr():
    """
    The PdfProcessor's OCR fallback and the standalone .png processor
    should be the SAME ImageProcessor instance -- not two separately
    constructed ones -- so there's only one OCR code path to reason about.
    """
    router = build_default_router()
    png_processor = router._processors[".png"]
    pdf_processor = router._processors[".pdf"]

    assert isinstance(png_processor, ImageProcessor)
    assert isinstance(pdf_processor, PdfProcessor)
    assert pdf_processor._ocr is png_processor
