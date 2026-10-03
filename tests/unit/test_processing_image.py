import io

import pytest

import app.processing.image as image_module
from app.processing.image import ImageProcessor
from app.sharepoint.files import DownloadedFile


def _tiny_png_bytes() -> bytes:
    from PIL import Image

    img = Image.new("RGB", (10, 10), color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_extract_returns_ocr_text_and_dimensions(monkeypatch):
    monkeypatch.setattr(image_module.pytesseract, "image_to_string", lambda img: "Detected text\n")

    f = DownloadedFile(item_id="1", filename="t.png", content=_tiny_png_bytes(), content_type=None)
    result = await ImageProcessor().extract(f)

    assert result.text == "Detected text"
    assert result.metadata == {"width": 10, "height": 10}


@pytest.mark.asyncio
async def test_extract_failure_is_recorded_not_raised(monkeypatch):
    def _boom(img):
        raise RuntimeError("tesseract binary not found")

    monkeypatch.setattr(image_module.pytesseract, "image_to_string", _boom)

    f = DownloadedFile(item_id="1", filename="t.png", content=_tiny_png_bytes(), content_type=None)
    result = await ImageProcessor().extract(f)

    assert result.text == ""
    assert result.warnings


@pytest.mark.asyncio
async def test_ocr_pdf_pages_only_rasterizes_requested_pages(monkeypatch):
    calls = []

    def _fake_convert(pdf_bytes, first_page, last_page):
        calls.append((first_page, last_page))
        return ["fake-image-object"]

    monkeypatch.setattr(image_module, "convert_from_bytes", _fake_convert)
    monkeypatch.setattr(image_module.pytesseract, "image_to_string", lambda img: f"OCR for {img}")

    results = await ImageProcessor().ocr_pdf_pages(b"fake pdf", [3, 7])

    assert calls == [(3, 3), (7, 7)]  # one call per page, not a full-document rasterize
    assert results[3] == "OCR for fake-image-object"
    assert results[7] == "OCR for fake-image-object"


@pytest.mark.asyncio
async def test_ocr_pdf_pages_failure_on_one_page_does_not_block_others(monkeypatch):
    def _fake_convert(pdf_bytes, first_page, last_page):
        if first_page == 2:
            raise RuntimeError("poppler failed on this page")
        return ["ok-image"]

    monkeypatch.setattr(image_module, "convert_from_bytes", _fake_convert)
    monkeypatch.setattr(image_module.pytesseract, "image_to_string", lambda img: "text")

    results = await ImageProcessor().ocr_pdf_pages(b"fake pdf", [1, 2, 3])

    assert results == {1: "text", 3: "text"}  # page 2 simply absent, not a crash
