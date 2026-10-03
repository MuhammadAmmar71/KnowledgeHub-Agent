from app.embeddings.text import build_embedding_text


def test_heading_and_document_name_are_prefixed():
    text = build_embedding_text("Body text.", {"name": "Guide.md", "heading": "Release Branches"})
    assert text == "Guide.md > Release Branches\nBody text."


def test_page_slide_and_sheet_labels():
    assert build_embedding_text("x", {"name": "a.pdf", "page_number": 3}) == "a.pdf > Page 3\nx"
    assert build_embedding_text("x", {"name": "b.pptx", "slide_number": 2}) == "b.pptx > Slide 2\nx"
    assert build_embedding_text("x", {"name": "c.xlsx", "sheet_name": "Q1"}) == "c.xlsx > Sheet Q1\nx"


def test_no_context_leaves_content_unchanged():
    assert build_embedding_text("Just content.", {}) == "Just content."


def test_name_only():
    assert build_embedding_text("x", {"name": "notes.txt"}) == "notes.txt\nx"
