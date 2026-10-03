import uuid

from app.chunking.chunker import Chunker


DOC_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def test_plain_text_with_no_markers_becomes_one_chunk():
    chunker = Chunker(chunk_size=800, chunk_overlap=120)
    chunks = chunker.chunk("Just a short plain paragraph with no structure.", DOC_ID, "hash1")
    assert len(chunks) == 1
    assert chunks[0].metadata.get("heading") is None
    assert chunks[0].chunk_index == 0


def test_empty_text_produces_no_chunks():
    chunker = Chunker()
    assert chunker.chunk("", DOC_ID, "hash1") == []
    assert chunker.chunk("   \n\n  ", DOC_ID, "hash1") == []


def test_markdown_headings_create_separate_sections_with_metadata():
    text = "# First Heading\nContent under first.\n\n## Second Heading\nContent under second."
    chunker = Chunker(chunk_size=800, chunk_overlap=0)
    chunks = chunker.chunk(text, DOC_ID, "hash1")

    assert len(chunks) == 2
    assert chunks[0].metadata["heading"] == "First Heading"
    assert "Content under first" in chunks[0].content
    assert chunks[1].metadata["heading"] == "Second Heading"
    assert "Content under second" in chunks[1].content


def test_pdf_page_markers_populate_page_number_metadata():
    text = "[PAGE 1]\nFirst page text.\n\n[PAGE 2]\nSecond page text."
    chunker = Chunker(chunk_size=800, chunk_overlap=0)
    chunks = chunker.chunk(text, DOC_ID, "hash1")

    assert len(chunks) == 2
    assert chunks[0].metadata["page_number"] == 1
    assert chunks[1].metadata["page_number"] == 2


def test_pptx_slide_markers_populate_slide_number_metadata():
    text = "--- SLIDE 1 ---\nTitle slide.\n\n--- SLIDE 2 ---\nSecond slide content."
    chunker = Chunker(chunk_size=800, chunk_overlap=0)
    chunks = chunker.chunk(text, DOC_ID, "hash1")

    assert len(chunks) == 2
    assert chunks[0].metadata["slide_number"] == 1
    assert chunks[1].metadata["slide_number"] == 2


def test_xlsx_sheet_markers_populate_sheet_name_metadata():
    text = "WORKBOOK: report.xlsx\n\nSHEET: Summary\nA | B\n1 | 2\n\nSHEET: Details\nX | Y\n3 | 4"
    chunker = Chunker(chunk_size=800, chunk_overlap=0)
    chunks = chunker.chunk(text, DOC_ID, "hash1")

    sheet_names = [c.metadata.get("sheet_name") for c in chunks]
    assert "Summary" in sheet_names
    assert "Details" in sheet_names


def test_oversized_section_is_split_with_overlap():
    # A single paragraph well over chunk_size, no sentence punctuation
    # breaks within a single "paragraph" grouping to force the splitter
    # through its paragraph -> sentence fallback.
    long_text = ("This is one sentence of reasonable length. " * 40).strip()
    chunker = Chunker(chunk_size=200, chunk_overlap=20)
    chunks = chunker.chunk(long_text, DOC_ID, "hash1")

    assert len(chunks) > 1
    for chunk in chunks:
        # Overlap means a chunk can exceed chunk_size slightly, but not
        # wildly -- this guards against the splitter degenerating into one
        # giant chunk or zero chunks.
        assert len(chunk.content) <= 200 + 20 + 50
    # Overlap actually overlaps: the tail of chunk N appears at the start of chunk N+1.
    assert chunks[0].content[-20:] in chunks[1].content


def test_chunk_indices_are_sequential_across_sections():
    text = "# Heading One\nShort.\n\n# Heading Two\nAlso short."
    chunker = Chunker(chunk_size=800, chunk_overlap=0)
    chunks = chunker.chunk(text, DOC_ID, "hash1")
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    for chunk in chunks:
        assert chunk.metadata["chunk_index"] == chunk.chunk_index


def test_base_metadata_is_merged_into_every_chunk():
    chunker = Chunker()
    base_metadata = {"source_type": "file", "sharepoint_item_id": "abc123", "name": "report.md"}
    chunks = chunker.chunk("# Heading\nSome text.", DOC_ID, "hash1", base_metadata)
    assert len(chunks) == 1
    assert chunks[0].metadata["sharepoint_item_id"] == "abc123"
    assert chunks[0].metadata["name"] == "report.md"
    assert chunks[0].metadata["heading"] == "Heading"


def test_chunk_ids_are_deterministic_for_same_input():
    chunker = Chunker()
    chunks_a = chunker.chunk("Some stable content here.", DOC_ID, "hash1")
    chunks_b = chunker.chunk("Some stable content here.", DOC_ID, "hash1")
    assert [c.id for c in chunks_a] == [c.id for c in chunks_b]


def test_chunk_ids_change_when_content_hash_changes():
    """
    Spec section 34/57: a genuine content change must produce entirely new
    chunk IDs, so the old chunk rows become naturally orphaned and the
    delete-then-insert replacement in ChunkRepository.replace_chunks is safe.
    """
    chunker = Chunker()
    chunks_old = chunker.chunk("Some content.", DOC_ID, "hash_old")
    chunks_new = chunker.chunk("Some content.", DOC_ID, "hash_new")
    assert {c.id for c in chunks_old}.isdisjoint({c.id for c in chunks_new})


def test_chunk_ids_differ_across_documents_with_same_content():
    chunker = Chunker()
    other_doc_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    chunks_a = chunker.chunk("Identical text.", DOC_ID, "hash1")
    chunks_b = chunker.chunk("Identical text.", other_doc_id, "hash1")
    assert {c.id for c in chunks_a}.isdisjoint({c.id for c in chunks_b})


def test_token_count_is_positive_and_roughly_proportional_to_length():
    chunker = Chunker()
    short = chunker.chunk("Short.", DOC_ID, "hash1")[0]
    long_chunks = chunker.chunk("A much longer sentence with quite a few more words in it.", DOC_ID, "hash1")
    assert short.token_count >= 1
    assert long_chunks[0].token_count > short.token_count
