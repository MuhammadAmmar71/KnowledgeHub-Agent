import io

import pytest

from app.processing.docx import DocxProcessor
from app.processing.pptx import PptxProcessor
from app.processing.xlsx import XlsxProcessor
from app.sharepoint.files import DownloadedFile


def _build_docx_bytes() -> bytes:
    from docx import Document as DocxDocument

    doc = DocxDocument()
    doc.add_heading("Main Heading", level=1)
    doc.add_paragraph("A regular paragraph of body text.")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "A1"
    table.cell(0, 1).text = "B1"
    table.cell(1, 0).text = "A2"
    table.cell(1, 1).text = "B2"

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _build_pptx_bytes() -> bytes:
    from pptx import Presentation

    prs = Presentation()
    slide_layout = prs.slide_layouts[1]  # title + content
    slide = prs.slides.add_slide(slide_layout)
    slide.shapes.title.text = "Slide One Title"
    body = slide.placeholders[1]
    body.text_frame.text = "Body bullet text"
    slide.notes_slide.notes_text_frame.text = "Speaker notes here"

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _build_xlsx_bytes() -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["Name", "Score"])
    ws.append(["Alice", 90])
    ws.append(["Bob", 85])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_docx_extracts_headings_paragraphs_and_tables():
    content = _build_docx_bytes()
    f = DownloadedFile(item_id="1", filename="t.docx", content=content, content_type=None)
    result = await DocxProcessor().extract(f)

    assert "Main Heading" in result.text
    assert "regular paragraph" in result.text
    assert "A1" in result.text and "B2" in result.text
    assert "[TABLE]" in result.text


@pytest.mark.asyncio
async def test_docx_invalid_bytes_reports_warning_not_crash():
    f = DownloadedFile(item_id="1", filename="bad.docx", content=b"not a real docx file", content_type=None)
    result = await DocxProcessor().extract(f)
    assert result.text == ""
    assert result.warnings


@pytest.mark.asyncio
async def test_pptx_extracts_slide_text_and_notes_with_slide_count():
    content = _build_pptx_bytes()
    f = DownloadedFile(item_id="1", filename="t.pptx", content=content, content_type=None)
    result = await PptxProcessor().extract(f)

    assert "Slide One Title" in result.text
    assert "Body bullet text" in result.text
    assert "[NOTES] Speaker notes here" in result.text
    assert "SLIDE 1" in result.text
    assert result.metadata["slide_count"] == 1


@pytest.mark.asyncio
async def test_xlsx_extracts_sheet_rows_and_skips_blank_rows():
    content = _build_xlsx_bytes()
    f = DownloadedFile(item_id="1", filename="t.xlsx", content=content, content_type=None)
    result = await XlsxProcessor().extract(f)

    assert "SHEET: Sheet1" in result.text
    assert "Alice" in result.text and "90" in result.text
    assert "Bob" in result.text
