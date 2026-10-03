import pytest

from app.processing.pdf import PdfProcessor
from app.sharepoint.files import DownloadedFile


class _FakePage:
    def __init__(self, text: str, raise_error: bool = False):
        self._text = text
        self._raise = raise_error

    def extract_text(self) -> str:
        if self._raise:
            raise RuntimeError("simulated extraction failure")
        return self._text


class _FakeReader:
    def __init__(self, pages: list[_FakePage]):
        self.pages = pages


def _file() -> DownloadedFile:
    return DownloadedFile(item_id="1", filename="t.pdf", content=b"fake pdf bytes", content_type=None)


@pytest.mark.asyncio
async def test_pages_with_good_text_layer_need_no_ocr():
    pages = [_FakePage("This page has plenty of real extractable text content.")]
    processor = PdfProcessor(ocr_processor=None, reader_factory=lambda _b: _FakeReader(pages))

    result = await processor.extract(_file())

    assert "plenty of real extractable text" in result.text
    assert result.metadata["page_count"] == 1
    assert not any("OCR" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_sparse_page_triggers_ocr_fallback_when_configured():
    pages = [_FakePage("")]  # empty text layer -> needs OCR

    class _StubOcr:
        async def ocr_pdf_pages(self, pdf_bytes: bytes, page_numbers: list[int]) -> dict[int, str]:
            assert page_numbers == [1]
            return {1: "Text recovered via OCR"}

    processor = PdfProcessor(ocr_processor=_StubOcr(), reader_factory=lambda _b: _FakeReader(pages))
    result = await processor.extract(_file())

    assert "Text recovered via OCR" in result.text
    assert any("OCR fallback used" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_sparse_page_without_ocr_configured_is_flagged_not_silently_empty():
    pages = [_FakePage("")]
    processor = PdfProcessor(ocr_processor=None, reader_factory=lambda _b: _FakeReader(pages))
    result = await processor.extract(_file())

    assert "no text layer, OCR not configured" in result.text
    assert any("no OCR processor was configured" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_ocr_failure_is_recorded_not_raised():
    pages = [_FakePage("")]

    class _FailingOcr:
        async def ocr_pdf_pages(self, pdf_bytes: bytes, page_numbers: list[int]) -> dict[int, str]:
            raise RuntimeError("tesseract not installed")

    processor = PdfProcessor(ocr_processor=_FailingOcr(), reader_factory=lambda _b: _FakeReader(pages))
    result = await processor.extract(_file())

    assert "(OCR unavailable)" in result.text
    assert any("OCR fallback failed" in w for w in result.warnings)


@pytest.mark.asyncio
async def test_per_page_extraction_error_does_not_crash_whole_document():
    pages = [_FakePage("Good page one has plenty of real text content."), _FakePage("", raise_error=True)]
    processor = PdfProcessor(ocr_processor=None, reader_factory=lambda _b: _FakeReader(pages))
    result = await processor.extract(_file())

    assert "Good page one has plenty of real text content." in result.text
    assert any("text extraction failed" in w for w in result.warnings)
    assert result.metadata["page_count"] == 2


@pytest.mark.asyncio
async def test_invalid_pdf_bytes_reports_warning_not_crash():
    def broken_factory(_b):
        raise ValueError("not a PDF")

    processor = PdfProcessor(reader_factory=broken_factory)
    result = await processor.extract(_file())
    assert result.text == ""
    assert result.warnings
