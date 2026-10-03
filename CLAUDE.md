# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Requirement specification document 

Please refer to this document Auximo_Knowledge_Agent_Implementation_Spec.md as the main requirement specification document, but we can have some changes in it to accomplish the project. 

We are completing the project in phases and till now we have completed 9 phases, these are as follows:
Phase 1 — Foundation: Set up authentication to Microsoft Graph and built the core client for talking to SharePoint. Proved we could reliably discover every file in the document library, including pagination and retries.
Phase 2 — Document Tracking: Added a database to remember what's been seen, and built real file downloading with content hashing. Made the whole sync process idempotent — reruns don't duplicate or waste work.
Phase 3 — Delta Sync: Switched from re-scanning everything every time to using Microsoft's official "what changed" feed. Cut sync time from ~34 seconds to ~2.5 seconds by only processing actual changes.
Phase 4 — SharePoint Pages: Extended tracking to SharePoint pages (not just files), including a permission capability check first. Confirmed our existing access level was enough and verified the extracted page text was clean.
Phase 5 — Content Extraction: Built readers for all 10 file types (Word, PDF, Excel, PowerPoint, images, etc.), including OCR for scanned content. Added a preview tool so we could check extraction quality before trusting it.
Phase 6 — Chunking: Split extracted text into smaller, structured pieces (by heading, page, or slide) ready for search. Wired this directly into the sync process, so new or changed content gets chunked automatically.
Phase 7 — Embeddings: Local sentence-transformers model (BAAI/bge-small-en-v1.5, 384 dims) behind an `EmbeddingProvider` interface, with vectors stored in a pgvector column on `chunks`. Runs as a separate batch stage after each sync, moving documents CHUNKED → INDEXED.
Phase 8 — Search: HNSW vector index plus a Postgres full-text column on `chunks`, hybrid retrieval (semantic + keyword, merged with Reciprocal Rank Fusion) with source/extension/path filters, exposed as a CLI (`python -m app.cli search`) rather than an HTTP endpoint.
Phase 9 — RAG answers: `python -m app.cli ask` retrieves chunks, groups them into numbered sources, and asks GPT-4o (company Azure OpenAI resource, v1 Responses API) to answer only from them with [n] citations and SharePoint URLs.

## What this is

A FastAPI service that syncs a single SharePoint site (document library + Site Pages) via Microsoft Graph into Postgres, extracts text, chunks it, embeds the chunks into pgvector, searches them, and answers questions over them with an LLM. It is built phase-by-phase against `Auximo_Knowledge_Agent_Implementation_Spec.md` (the authoritative design doc; code comments cite it as "spec section N" and "Rule N"). Phases 1–9 are implemented (auth, discovery, initial/delta sync, pages, extraction, chunking, embeddings, search, RAG answers). Phase 10 (production hardening) is next. `README.md` is stale and only describes Phase 1.

## Commands

The virtualenv is `venv/` (not `.venv` as README says); there is no bare `python` on PATH.

```bash
venv/bin/python -m pytest                                    # full suite (asyncio_mode=auto)
venv/bin/python -m pytest tests/unit/test_chunker.py         # one file
venv/bin/python -m pytest tests/unit/test_chunker.py::test_name  # one test
docker compose up -d db                                      # Postgres 16 + pgvector on :5432
venv/bin/alembic upgrade head                                # migrations (URL comes from Settings.database_url, not alembic.ini)
venv/bin/uvicorn app.main:app --reload --port 8000
venv/bin/python -m app.cli search "query" [-k 10] [--mode semantic] [--type page] [--ext .md] [--path 02_Development] [--json]
venv/bin/python -m app.cli ask "question" [same filters] [--show-context] [--json]   # needs AZURE_OPENAI_API_KEY
venv/bin/python -m app.cli reprocess   # after changing extraction/chunking: marks docs PENDING; then POST /sync?mode=full and POST /sync/pages
```

Install CPU-only torch before the requirements, or pip pulls multi-GB CUDA wheels: `venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu`. The embedding model downloads (~130MB) on first use, not at startup; after that it loads from the local HuggingFace cache (`local_files_only=True` first), so search/ask/sync work without reaching huggingface.co.

No linter/formatter is configured. Config comes from `.env` (TENANT_ID, CLIENT_ID, CLIENT_SECRET, SHAREPOINT_SITE_ID, SHAREPOINT_DRIVE_ID required). PDF/PNG OCR needs system `tesseract` and `poppler`.

## Architecture

Pipeline: `GraphAuth` (MSAL app-only) → `GraphClient` (pagination, retry/backoff on 429/5xx, bounded concurrency) → discovery/delta/pages (`app/sharepoint/`) → `app/sync/manager.py` (orchestration) → `ProcessorRouter` (`app/processing/`, extension → processor) → `Chunker` → repositories (`app/sync/state.py`). Then, as a separate stage: `app/embeddings/indexer.py` → `EmbeddingProvider` → `chunks.embedding`. Retrieval: `app/retrieval/search.py` (`search_chunks`). Answers: `app/rag/answer.py` (`answer_question`) → `LLMProvider` (`app/llm/`, implemented by `AzureOpenAIResponsesClient`). Both are driven from `app/cli.py`.

Key cross-cutting design points:

- **Sync is decoupled from extraction (Rule 10/11).** Every function in `sync/manager.py` takes `router`, `chunker`, `chunk_repo` as optional args. When all three are supplied, newly added/modified content is extracted and chunked from the already-downloaded bytes; when omitted, sync only downloads, hashes and tracks documents. Embedding is deliberately *not* wired into sync at all — see next point.
- **Embedding is a separate stage selected by status.** Sync leaves new/changed documents at `CHUNKED`; `embed_pending_documents` picks up `CHUNKED`/`EMBEDDING`/`EMBEDDING_FAILED` documents, plus `INDEXED` ones whose chunks carry a different `embedding_model` (model change → re-embed). It runs after every sync (`EMBED_AFTER_SYNC`) and via `POST /embeddings/run[?reembed=all]`; failures stay per-document and never affect sync status or the delta cursor. It commits per document, and holds plain `_DocumentRef` values rather than ORM objects because a rollback expires them (async sessions then raise on attribute access).
- **Embedding dimension is part of the schema.** `EMBEDDING_DIMENSIONS` in `app/db/models.py` fixes `vector(384)`; switching `EMBEDDING_MODEL_NAME` to a model with another dimension needs a new migration, and the indexer refuses to run on a mismatch.
- **Queries and passages are embedded differently.** Use `provider.embed_query()` for search queries (prepends `EMBEDDING_QUERY_INSTRUCTION`, which bge models require) and `embed()` for chunk text; mixing them up quietly degrades retrieval.
- **Search** (`search_chunks`) only compares chunks whose `embedding_model` matches the active model and whose document isn't deleted, and takes name/URL/path from `documents` (current values), not chunk metadata. Hybrid mode fuses separate semantic and keyword candidate lists by rank (RRF), so their raw scores never need calibrating. The semantic query sets `hnsw.iterative_scan` (pgvector ≥ 0.8) so filtered searches still return `top_k` rows. `chunks.content_tsv` is a Postgres generated column — never write it from the app.
- **LLM access** is the company Azure OpenAI resource via the v1 Responses API (`POST {AZURE_OPENAI_ENDPOINT}/responses`, `api-key` header, `model` = deployment name; no `api-version`). Config: `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY` (SecretStr, optional so sync/search work without it), `AZURE_OPENAI_DEPLOYMENT`. The key must never be logged or put in error messages. The client is plain `httpx`, not the openai SDK.
- **RAG grounding rules** (spec §38) live in `SYSTEM_INSTRUCTIONS` in `app/rag/answer.py`; citation numbers are per *document* (chunks grouped by `group_sources`), and the LLM is not called at all when retrieval returns nothing.
- **Chunk overlap** is snapped to a sentence or word boundary (`_overlap_tail`) and long unpunctuated text splits between words (`_split_by_words`); only single tokens longer than `chunk_size` are hard-cut.
- **Identity and change detection.** Documents are keyed by `(source_type, sharepoint_item_id)`, never filename. Changed vs. unchanged is decided by SHA-256 content hash; unchanged items only get a metadata touch and are never re-chunked — unless their status shows processing never completed (`_INCOMPLETE_PROCESSING_STATUSES` in `sync/manager.py`: `PENDING`, `DOWNLOAD_FAILED`, `EXTRACTION_FAILED`, `CHUNKING`, `CHUNKING_FAILED`), in which case they're extracted and chunked anyway.
- **Deterministic chunk IDs.** `uuid5(document_id:content_hash:chunk_index)`, so reruns produce identical IDs and content changes produce entirely new ones; `replace_chunks` swaps a document's chunks wholesale.
- **Delta cursor safety (Rule 8).** `run_delta_sync` only persists the new deltaLink via `record_success` when the pass had zero item failures; otherwise the old cursor is kept and the batch is retried (idempotent via hashing). `POST /sync?mode=auto` runs a full walk + establishes a baseline if no cursor exists, otherwise delta; `mode=full` forces the walk.
- **Per-item failure isolation.** Each item's DB writes are guarded with `repo.rollback()` so one bad item is recorded as failed (with a `ProcessingStatus` like `DOWNLOAD_FAILED`/`EXTRACTION_FAILED`) without poisoning the session for the rest of the run. This only works because sync commits per item (`repo.commit()`), and commits a document row *before* chunking it: `rollback()` discards everything uncommitted in the session, so without those commits one failure would silently erase earlier items in the run. Any new write path must keep this pattern, and must read ORM ids into locals before a possible rollback (rollback expires instances; async sessions raise on expired attribute access). `tests/unit/test_sync_transactions.py` has repository doubles with real commit/rollback semantics for testing this.
- **Repository protocols with two implementations.** `DocumentRepository`, `SyncStateRepository`, `ChunkRepository` each have a `SqlAlchemy*` (production) and `InMemory*` (tests) implementation in `app/sync/state.py`. New persistence behavior must be added to both.
- **Chunker structure markers.** Processors emit text with markers the chunker splits on: `[PAGE n]` (PDF), `--- SLIDE n ---` (PPTX), `SHEET: name` (XLSX), `#`/`##` headings (Markdown, DOCX). The marker line is removed from chunk content and stored in chunk metadata (`page_number`, `slide_number`, `sheet_name`, `heading`). New processors should emit these markers rather than invent new ones.
- **Sync runs execute as FastAPI `BackgroundTasks`.** The endpoint creates a `SyncRun` row and returns its id immediately; `_execute_*_sync` in `app/main.py` opens its own session.
- **Pages** (`app/sharepoint/pages.py`) may need `Sites.Read.All` rather than `Sites.Selected`; `/health/pages` runs a capability check first. Don't broaden permissions automatically (Rule 6).

## Testing conventions

All tests are unit tests with no network or DB: Graph is mocked via `httpx.MockTransport` passed to `GraphClient(FakeGraphAuth(), settings, transport=...)`, persistence uses the `InMemory*` repositories, and embeddings use `FakeEmbeddingProvider` (deterministic, no torch). Use the `settings` fixture from `tests/conftest.py` (short retry backoff keeps tests fast).

## Spec rules to respect

From spec section 61: don't hardcode the SharePoint folder tree; don't store access tokens or temporary download URLs (httpx logging is raised to WARNING in `main.py` for this reason); don't silently swallow sync errors; don't duplicate chunks on rerun; use bounded async concurrency; prefer Graph v1.0 APIs; don't rewrite working components unnecessarily.
