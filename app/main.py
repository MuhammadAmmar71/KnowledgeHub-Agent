"""
FastAPI entrypoint -- Phases 1-7: Graph auth/discovery, persistent state,
initial/delta/page sync, extraction, chunking, and the embedding stage.
Vector search and RAG (Phases 8-9) are not wired up yet.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select

from app.auth.graph_auth import GraphAuth, GraphAuthError
from app.chunking.chunker import Chunker
from app.config import get_settings
from app.db.database import build_engine, build_session_factory
from app.db.models import Chunk as ChunkModel
from app.db.models import Document, SourceType, SyncRun, SyncRunStatus, SyncRunType
from app.embeddings.indexer import embed_pending_documents
from app.embeddings.sentence_transformer import SentenceTransformerProvider
from app.processing.router import UnsupportedExtensionError, build_default_router
from app.sharepoint.client import GraphApiError, GraphClient
from app.sharepoint.drives import DiscoveredFile, check_drive_health, check_site_health, walk_drive
from app.sharepoint.files import download_file
from app.sharepoint.pages import check_page_capability, get_page_content, normalize_page_content
from app.sync.manager import establish_delta_baseline, run_delta_sync, run_initial_sync, run_page_sync
from app.sync.state import SqlAlchemyChunkRepository, SqlAlchemyDocumentRepository, SqlAlchemySyncStateRepository

DOCUMENT_LIBRARY_SOURCE = "document_library"
# One shared router instance -- processors (notably ImageProcessor's
# Tesseract path) have no per-request state worth recreating every call.
_processing_router = build_default_router()
# Same reasoning for the embedding provider, plus it's expensive: the model
# is loaded once (lazily, on first embed) and reused for every run.
_embedding_provider: SentenceTransformerProvider | None = None
# One embedding run at a time -- an auto run after sync and a manual
# POST /embeddings/run would otherwise select and embed the same documents.
_embedding_lock = asyncio.Lock()

logging.basicConfig(level=logging.INFO)
# httpx logs every request URL at INFO. Following the /content redirect means
# the target is a temporary download URL carrying auth material in its query
# string, which must never be logged (spec section 50).
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("knowledge_agent.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    engine = build_engine(settings)
    app.state.session_factory = build_session_factory(engine)
    try:
        yield
    finally:
        await engine.dispose()


app = FastAPI(title="Auximo Knowledge Agent", version="0.7.0-phase7", lifespan=lifespan)


def _build_graph_client() -> GraphClient:
    settings = get_settings()
    auth = GraphAuth(settings)
    return GraphClient(auth, settings)


def _build_chunker() -> Chunker:
    settings = get_settings()
    return Chunker(chunk_size=settings.chunk_size, chunk_overlap=settings.chunk_overlap)


def _get_embedding_provider() -> SentenceTransformerProvider:
    global _embedding_provider
    if _embedding_provider is None:
        settings = get_settings()
        _embedding_provider = SentenceTransformerProvider(
            settings.embedding_model_name,
            device=settings.embedding_device,
            batch_size=settings.embedding_batch_size,
            query_instruction=settings.embedding_query_instruction,
        )
    return _embedding_provider


async def _execute_embedding_run(reembed_all: bool = False) -> None:
    """
    Runs the embedding stage in its own session. Never raises: embedding
    failures are tracked per document (EMBEDDING_FAILED) and retried by the
    next run, so they never affect a sync run's status or its delta cursor.
    """
    async with _embedding_lock:
        session_factory = app.state.session_factory
        async with session_factory() as session:
            try:
                counters = await embed_pending_documents(
                    doc_repo=SqlAlchemyDocumentRepository(session),
                    chunk_repo=SqlAlchemyChunkRepository(session),
                    provider=_get_embedding_provider(),
                    batch_size=get_settings().embedding_batch_size,
                    reembed_all=reembed_all,
                )
                logger.info(
                    "embedding_stage_completed indexed=%d failed=%d chunks=%d",
                    counters.documents_indexed, counters.documents_failed, counters.chunks_embedded,
                )
            except Exception:  # noqa: BLE001 -- background job; log, don't crash the worker
                logger.exception("embedding_stage_crashed")
                await session.rollback()


async def _embed_after_sync() -> None:
    if get_settings().embed_after_sync:
        await _execute_embedding_run()


@app.get("/health")
async def health() -> dict:
    """Liveness check -- does not touch Graph or the network."""
    return {"status": "ok", "phase": "1"}


@app.get("/health/graph")
async def health_graph() -> dict:
    """
    Confirms: app-only auth succeeds, the configured site is reachable,
    and the configured drive is reachable. Never returns secrets/tokens.
    """
    settings = get_settings()
    client = _build_graph_client()
    try:
        site_info = await check_site_health(client, settings)
        drive_info = await check_drive_health(client, settings)
        return {"status": "ok", "site": site_info, "drive": drive_info}
    except GraphAuthError as exc:
        logger.error("health_graph_auth_failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except GraphApiError as exc:
        logger.error("health_graph_api_failed status=%s", exc.status_code)
        raise HTTPException(
            status_code=502, detail="Microsoft Graph request failed. See server logs."
        ) from exc
    finally:
        await client.aclose()


@app.get("/discovery/preview")
async def discovery_preview() -> dict:
    """
    Manual development endpoint (spec section 69): runs a full recursive
    discovery pass and returns counts only -- not the full file listing --
    so this is safe to call against a real, large drive without flooding
    the response.
    """
    settings = get_settings()
    client = _build_graph_client()
    try:
        result = await walk_drive(client, settings.sharepoint_drive_id)
        return {
            "folders_discovered": result.folders_visited,
            "files_discovered": len(result.files) + len(result.skipped_unsupported),
            "supported_files": len(result.files),
            "unsupported_files": len(result.skipped_unsupported),
        }
    except GraphAuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except GraphApiError as exc:
        raise HTTPException(
            status_code=502, detail="Microsoft Graph request failed. See server logs."
        ) from exc
    finally:
        await client.aclose()


@app.get("/health/pages")
async def health_pages() -> dict:
    """
    Capability test ONLY (spec section 7) -- confirms whether the app's
    current Sites.Selected permission is actually sufficient to list
    SharePoint pages. Side-effect free: never lists pages, never writes to
    the database, safe to call at any time regardless of whether page sync
    has ever been attempted.
    """
    settings = get_settings()
    client = _build_graph_client()
    try:
        result = await check_page_capability(client, settings.sharepoint_site_id)
        return {
            "pages_capability_available": result.available,
            "http_status": result.http_status,
            "detail": result.detail,
        }
    except GraphAuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        await client.aclose()


@app.get("/debug/pages/{page_id}/preview", response_class=PlainTextResponse)
async def preview_page_content(page_id: str) -> str:
    """
    Read-only, no database writes: fetches ONE page live from Graph and
    returns its normalized TITLE/DESCRIPTION/TEXT/WEB URL representation
    as plain text, so you can eyeball extraction quality directly rather
    than trusting a hash match. page_id is the Graph page id -- get one
    from GET /documents (look for source_type: "page", use its
    sharepoint_item_id, not its internal id).
    """
    settings = get_settings()
    client = _build_graph_client()
    try:
        detail = await get_page_content(client, settings.sharepoint_site_id, page_id)
        return normalize_page_content(detail)
    except GraphAuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except GraphApiError as exc:
        status = exc.status_code if exc.status_code and exc.status_code < 500 else 502
        raise HTTPException(
            status_code=status,
            detail={
                "message": f"Graph returned an error fetching page {page_id}.",
                "graph_status": exc.status_code,
                "graph_error_body": exc.response_body,
            },
        ) from exc
    finally:
        await client.aclose()


@app.get("/debug/files/{item_id}/preview", response_class=PlainTextResponse)
async def preview_file_content(item_id: str) -> str:
    """
    Read-only, no database writes: downloads ONE tracked file live from
    Graph, routes it through the same processor a real Phase 6 chunking
    pass would use, and returns the extracted text (plus any warnings) as
    plain text -- same purpose as /debug/pages/{page_id}/preview, applied
    to files. item_id is the Graph sharepoint_item_id (from GET /documents,
    source_type: "file"), not the internal document id.
    """
    settings = get_settings()
    session_factory = app.state.session_factory

    async with session_factory() as session:
        repo = SqlAlchemyDocumentRepository(session)
        document = await repo.get_by_sharepoint_item_id(SourceType.file, item_id)

    if document is None:
        raise HTTPException(
            status_code=404,
            detail=f"No tracked file document with sharepoint_item_id={item_id}. "
                    "Check GET /documents for source_type: \"file\" entries.",
        )

    # download_file only needs item_id + name off a DiscoveredFile -- this
    # stub carries just enough to reuse that function without re-walking
    # the drive or duplicating its download logic.
    discovered_stub = DiscoveredFile(
        item_id=document.sharepoint_item_id,
        name=document.name,
        extension=document.extension or "",
        size=document.size or 0,
        web_url=document.sharepoint_url or "",
        parent_item_id=document.parent_sharepoint_item_id,
        path=document.path,
        etag=document.etag,
        last_modified=document.modified_at.isoformat() if document.modified_at else None,
        is_supported=True,
    )

    client = _build_graph_client()
    try:
        downloaded = await download_file(client, settings.sharepoint_drive_id, discovered_stub)
        extracted = await _processing_router.extract(downloaded, document.extension or "")
    except UnsupportedExtensionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GraphAuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except GraphApiError as exc:
        status = exc.status_code if exc.status_code and exc.status_code < 500 else 502
        raise HTTPException(
            status_code=status,
            detail={
                "message": f"Graph returned an error downloading item {item_id}.",
                "graph_status": exc.status_code,
                "graph_error_body": exc.response_body,
            },
        ) from exc
    finally:
        await client.aclose()

    output = extracted.text
    if extracted.warnings:
        warning_lines = "".join(f"# WARNING: {w}\n" for w in extracted.warnings)
        output = f"{warning_lines}\n{output}"
    return output


def _serialize_chunks_preview(chunks: list, warnings: list[str]) -> dict:
    return {
        "chunk_count": len(chunks),
        "warnings": warnings,
        "chunks": [
            {
                "chunk_index": c.chunk_index,
                "token_count": c.token_count,
                "char_count": len(c.content),
                "metadata": c.metadata,
                # Full content is already viewable via the /preview endpoints;
                # here we're checking BOUNDARIES and METADATA, so a snippet
                # is enough and keeps the response readable.
                "content_preview": c.content[:300] + ("..." if len(c.content) > 300 else ""),
            }
            for c in chunks
        ],
    }


@app.get("/debug/files/{item_id}/chunks")
async def preview_file_chunks(item_id: str) -> dict:
    """
    Read-only, no database writes: downloads + extracts a tracked file
    exactly like preview_file_content, then runs it through the SAME
    Chunker the real sync pipeline uses, so you can inspect chunk
    boundaries, sizes, and metadata before trusting them enough to embed
    (Phase 7). Returns the chunk IDs the real pipeline would actually
    produce for this content, since the hash is computed the same way.
    """
    settings = get_settings()
    session_factory = app.state.session_factory

    async with session_factory() as session:
        repo = SqlAlchemyDocumentRepository(session)
        document = await repo.get_by_sharepoint_item_id(SourceType.file, item_id)

    if document is None:
        raise HTTPException(
            status_code=404,
            detail=f"No tracked file document with sharepoint_item_id={item_id}. "
                    "Check GET /documents for source_type: \"file\" entries.",
        )

    discovered_stub = DiscoveredFile(
        item_id=document.sharepoint_item_id,
        name=document.name,
        extension=document.extension or "",
        size=document.size or 0,
        web_url=document.sharepoint_url or "",
        parent_item_id=document.parent_sharepoint_item_id,
        path=document.path,
        etag=document.etag,
        last_modified=document.modified_at.isoformat() if document.modified_at else None,
        is_supported=True,
    )

    client = _build_graph_client()
    try:
        downloaded = await download_file(client, settings.sharepoint_drive_id, discovered_stub)
        extracted = await _processing_router.extract(downloaded, document.extension or "")
    except UnsupportedExtensionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except GraphAuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except GraphApiError as exc:
        status = exc.status_code if exc.status_code and exc.status_code < 500 else 502
        raise HTTPException(
            status_code=status,
            detail={"graph_status": exc.status_code, "graph_error_body": exc.response_body},
        ) from exc
    finally:
        await client.aclose()

    content_hash = document.content_hash or ""
    base_metadata = {
        "source_type": document.source_type,
        "sharepoint_item_id": document.sharepoint_item_id,
        "name": document.name,
        "path": document.path,
        "sharepoint_url": document.sharepoint_url,
    }
    chunks = _build_chunker().chunk(extracted.text, document.id, content_hash, base_metadata)
    return _serialize_chunks_preview(chunks, extracted.warnings)


@app.get("/debug/pages/{page_id}/chunks")
async def preview_page_chunks(page_id: str) -> dict:
    """Same purpose as preview_file_chunks, for a tracked SharePoint page."""
    settings = get_settings()
    session_factory = app.state.session_factory

    async with session_factory() as session:
        repo = SqlAlchemyDocumentRepository(session)
        document = await repo.get_by_sharepoint_item_id(SourceType.page, page_id)

    if document is None:
        raise HTTPException(
            status_code=404,
            detail=f"No tracked page document with sharepoint_item_id={page_id}. "
                    "Check GET /documents for source_type: \"page\" entries.",
        )

    client = _build_graph_client()
    try:
        detail = await get_page_content(client, settings.sharepoint_site_id, page_id)
        normalized_text = normalize_page_content(detail)
    except GraphAuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except GraphApiError as exc:
        status = exc.status_code if exc.status_code and exc.status_code < 500 else 502
        raise HTTPException(
            status_code=status,
            detail={"graph_status": exc.status_code, "graph_error_body": exc.response_body},
        ) from exc
    finally:
        await client.aclose()

    content_hash = document.content_hash or ""
    base_metadata = {
        "source_type": document.source_type,
        "sharepoint_item_id": document.sharepoint_item_id,
        "name": document.name,
        "path": document.path,
        "sharepoint_url": document.sharepoint_url,
    }
    chunks = _build_chunker().chunk(normalized_text, document.id, content_hash, base_metadata)
    return _serialize_chunks_preview(chunks, [])


async def _execute_initial_sync(sync_run_id: uuid.UUID) -> None:
    """
    Background job for POST /sync. Owns the full sync_run lifecycle:
    running -> success/partial/failed. Per spec section 41, the HTTP
    request that triggers this must not wait for it to finish.
    """
    settings = get_settings()
    client = _build_graph_client()
    session_factory = app.state.session_factory

    async with session_factory() as session:
        try:
            repo = SqlAlchemyDocumentRepository(session)
            chunk_repo = SqlAlchemyChunkRepository(session)
            chunker = _build_chunker()
            counters = await run_initial_sync(
                client=client,
                drive_id=settings.sharepoint_drive_id,
                site_id=settings.sharepoint_site_id,
                repo=repo,
                router=_processing_router,
                chunker=chunker,
                chunk_repo=chunk_repo,
            )

            # Establish a delta cursor now so the NEXT POST /sync can take
            # the cheap incremental path instead of repeating this full
            # walk. A failure here doesn't fail the sync_run itself -- the
            # initial sync already succeeded; worst case, the next run just
            # falls back to another full walk (safe, just not ideal).
            sync_state_repo = SqlAlchemySyncStateRepository(session)
            try:
                await establish_delta_baseline(client, settings.sharepoint_drive_id, sync_state_repo)
            except Exception:
                logger.exception("delta_baseline_establishment_failed sync_id=%s", sync_run_id)

            sync_run = await session.get(SyncRun, sync_run_id)
            sync_run.items_detected = counters.items_detected
            sync_run.items_added = counters.items_added
            sync_run.items_modified = counters.items_modified
            sync_run.items_deleted = counters.items_deleted
            sync_run.items_skipped = counters.items_skipped
            sync_run.items_failed = counters.items_failed
            sync_run.error_count = len(counters.errors)
            sync_run.error_summary = "; ".join(counters.errors[:20]) or None
            sync_run.status = (
                SyncRunStatus.success if not counters.has_failures else SyncRunStatus.partial
            )
            sync_run.completed_at = datetime.now(timezone.utc)
            await session.commit()
            logger.info(
                "sync_run_completed sync_id=%s status=%s added=%d modified=%d failed=%d skipped=%d",
                sync_run_id, sync_run.status, counters.items_added,
                counters.items_modified, counters.items_failed, counters.items_skipped,
            )
        except Exception as exc:  # noqa: BLE001 -- last-resort guard so a run always gets finalized
            logger.exception("sync_run_crashed sync_id=%s", sync_run_id)
            # The exception may have left the session's transaction aborted
            # (e.g. a Postgres error mid-flush) -- roll back before trying
            # to use the same session again, or this recovery path throws
            # its own PendingRollbackError on top of the original failure.
            await session.rollback()
            sync_run = await session.get(SyncRun, sync_run_id)
            if sync_run is not None:
                sync_run.status = SyncRunStatus.failed
                sync_run.error_summary = f"{type(exc).__name__}: {exc}"[:2000]
                sync_run.completed_at = datetime.now(timezone.utc)
                await session.commit()
        finally:
            await client.aclose()

    await _embed_after_sync()


async def _execute_delta_sync(sync_run_id: uuid.UUID) -> None:
    """
    Background job for the incremental path. Same lifecycle contract as
    _execute_initial_sync: always finalizes the sync_run row, rolls back
    before reusing a session that may have been left in an aborted
    transaction state, and never lets an unexpected exception escape
    silently.
    """
    settings = get_settings()
    client = _build_graph_client()
    session_factory = app.state.session_factory

    async with session_factory() as session:
        try:
            doc_repo = SqlAlchemyDocumentRepository(session)
            sync_state_repo = SqlAlchemySyncStateRepository(session)
            chunk_repo = SqlAlchemyChunkRepository(session)
            chunker = _build_chunker()
            counters = await run_delta_sync(
                client=client,
                drive_id=settings.sharepoint_drive_id,
                site_id=settings.sharepoint_site_id,
                doc_repo=doc_repo,
                sync_state_repo=sync_state_repo,
                source=DOCUMENT_LIBRARY_SOURCE,
                router=_processing_router,
                chunker=chunker,
                chunk_repo=chunk_repo,
            )

            sync_run = await session.get(SyncRun, sync_run_id)
            sync_run.items_detected = counters.items_detected
            sync_run.items_added = counters.items_added
            sync_run.items_modified = counters.items_modified
            sync_run.items_deleted = counters.items_deleted
            sync_run.items_skipped = counters.items_skipped
            sync_run.items_failed = counters.items_failed
            sync_run.error_count = len(counters.errors)
            sync_run.error_summary = "; ".join(counters.errors[:20]) or None
            sync_run.status = (
                SyncRunStatus.success if not counters.has_failures else SyncRunStatus.partial
            )
            sync_run.completed_at = datetime.now(timezone.utc)
            await session.commit()
            logger.info(
                "delta_sync_run_completed sync_id=%s status=%s added=%d modified=%d deleted=%d failed=%d skipped=%d",
                sync_run_id, sync_run.status, counters.items_added, counters.items_modified,
                counters.items_deleted, counters.items_failed, counters.items_skipped,
            )
        except Exception as exc:  # noqa: BLE001 -- last-resort guard so a run always gets finalized
            logger.exception("delta_sync_run_crashed sync_id=%s", sync_run_id)
            await session.rollback()
            sync_run = await session.get(SyncRun, sync_run_id)
            if sync_run is not None:
                sync_run.status = SyncRunStatus.failed
                sync_run.error_summary = f"{type(exc).__name__}: {exc}"[:2000]
                sync_run.completed_at = datetime.now(timezone.utc)
                await session.commit()
        finally:
            await client.aclose()

    await _embed_after_sync()


async def _execute_page_sync(sync_run_id: uuid.UUID) -> None:
    """
    Background job for POST /sync/pages. Runs the capability check first
    and stops cleanly (marks the run failed with a clear reason, does not
    proceed) if permissions aren't sufficient -- never attempts page
    listing on a permission it hasn't confirmed works (spec section 7,
    Rule 6: do not auto-escalate).
    """
    settings = get_settings()
    client = _build_graph_client()
    session_factory = app.state.session_factory

    async with session_factory() as session:
        try:
            capability = await check_page_capability(client, settings.sharepoint_site_id)
            if not capability.available:
                sync_run = await session.get(SyncRun, sync_run_id)
                if sync_run is not None:
                    sync_run.status = SyncRunStatus.failed
                    sync_run.error_summary = capability.detail
                    sync_run.completed_at = datetime.now(timezone.utc)
                    await session.commit()
                logger.error(
                    "page_sync_blocked_by_permissions sync_id=%s http_status=%s",
                    sync_run_id, capability.http_status,
                )
                return

            doc_repo = SqlAlchemyDocumentRepository(session)
            chunk_repo = SqlAlchemyChunkRepository(session)
            chunker = _build_chunker()
            counters = await run_page_sync(
                client, settings.sharepoint_site_id, doc_repo, chunker=chunker, chunk_repo=chunk_repo
            )

            sync_run = await session.get(SyncRun, sync_run_id)
            sync_run.items_detected = counters.items_detected
            sync_run.items_added = counters.items_added
            sync_run.items_modified = counters.items_modified
            sync_run.items_deleted = counters.items_deleted
            sync_run.items_skipped = counters.items_skipped
            sync_run.items_failed = counters.items_failed
            sync_run.error_count = len(counters.errors)
            sync_run.error_summary = "; ".join(counters.errors[:20]) or None
            sync_run.status = (
                SyncRunStatus.success if not counters.has_failures else SyncRunStatus.partial
            )
            sync_run.completed_at = datetime.now(timezone.utc)
            await session.commit()
            logger.info(
                "page_sync_run_completed sync_id=%s status=%s added=%d modified=%d deleted=%d failed=%d",
                sync_run_id, sync_run.status, counters.items_added,
                counters.items_modified, counters.items_deleted, counters.items_failed,
            )
        except Exception as exc:  # noqa: BLE001 -- last-resort guard so a run always gets finalized
            logger.exception("page_sync_run_crashed sync_id=%s", sync_run_id)
            await session.rollback()
            sync_run = await session.get(SyncRun, sync_run_id)
            if sync_run is not None:
                sync_run.status = SyncRunStatus.failed
                sync_run.error_summary = f"{type(exc).__name__}: {exc}"[:2000]
                sync_run.completed_at = datetime.now(timezone.utc)
                await session.commit()
        finally:
            await client.aclose()

    await _embed_after_sync()


@app.post("/sync/pages")
async def trigger_page_sync(background_tasks: BackgroundTasks) -> dict:
    """
    Deliberately a separate endpoint from POST /sync (spec section 7):
    page sync depends on a permission that may not be granted, and must
    never silently piggyback on, or block, document-library sync. Call
    GET /health/pages first if you just want to check capability without
    triggering a run.
    """
    session_factory = app.state.session_factory
    async with session_factory() as session:
        sync_run = SyncRun(id=uuid.uuid4(), sync_type=SyncRunType.pages, status=SyncRunStatus.running)
        session.add(sync_run)
        await session.commit()
        sync_id = sync_run.id

    background_tasks.add_task(_execute_page_sync, sync_id)
    return {"sync_id": str(sync_id), "status": "started", "sync_type": "pages"}


@app.post("/sync")
async def trigger_sync(background_tasks: BackgroundTasks, mode: str = "auto") -> dict:
    """
    Creates a sync_run row immediately and returns sync_id without waiting
    for the sync to finish (spec section 40/41).

    mode=auto (default), per spec section 64's daily-sync diagram: if no
    delta cursor exists yet, run the full recursive walk (Phase 2) and
    establish a cursor at the end; once a cursor exists, take the cheap
    incremental delta path (Phase 3).

    mode=full (spec section 53): always run the full walk and re-establish
    the cursor, regardless of existing state. Rebuild/repair path -- safe to
    run any time, since unchanged content is detected by hash and skipped.
    """
    if mode not in ("auto", "full"):
        raise HTTPException(status_code=400, detail="mode must be 'auto' or 'full'")

    session_factory = app.state.session_factory
    async with session_factory() as session:
        sync_state_repo = SqlAlchemySyncStateRepository(session)
        existing_delta_link = await sync_state_repo.get_delta_link(DOCUMENT_LIBRARY_SOURCE)
        run_full = mode == "full" or existing_delta_link is None
        sync_type = SyncRunType.initial if run_full else SyncRunType.delta

        sync_run = SyncRun(id=uuid.uuid4(), sync_type=sync_type, status=SyncRunStatus.running)
        session.add(sync_run)
        await session.commit()
        sync_id = sync_run.id

    if sync_type == SyncRunType.initial:
        background_tasks.add_task(_execute_initial_sync, sync_id)
    else:
        background_tasks.add_task(_execute_delta_sync, sync_id)

    return {"sync_id": str(sync_id), "status": "started", "sync_type": sync_type.value}


@app.get("/sync/latest")
async def get_latest_sync() -> dict:
    session_factory = app.state.session_factory
    async with session_factory() as session:
        stmt = select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1)
        result = await session.execute(stmt)
        sync_run = result.scalar_one_or_none()
        if sync_run is None:
            raise HTTPException(status_code=404, detail="no sync runs yet")
        return _serialize_sync_run(sync_run)


@app.get("/sync/{sync_id}")
async def get_sync_status(sync_id: uuid.UUID) -> dict:
    session_factory = app.state.session_factory
    async with session_factory() as session:
        sync_run = await session.get(SyncRun, sync_id)
        if sync_run is None:
            raise HTTPException(status_code=404, detail="sync_run not found")
        return _serialize_sync_run(sync_run)


@app.post("/embeddings/run")
async def trigger_embedding_run(background_tasks: BackgroundTasks, reembed: str = "pending") -> dict:
    """
    reembed=pending (default): embed everything CHUNKED / EMBEDDING_FAILED /
    embedded by a different model. reembed=all: re-embed every indexed
    document too (spec section 35's full re-embedding path).
    """
    if reembed not in ("pending", "all"):
        raise HTTPException(status_code=400, detail="reembed must be 'pending' or 'all'")
    background_tasks.add_task(_execute_embedding_run, reembed == "all")
    return {"status": "started", "reembed": reembed}


@app.get("/embeddings/status")
async def get_embedding_status() -> dict:
    session_factory = app.state.session_factory
    async with session_factory() as session:
        status_rows = await session.execute(
            select(Document.processing_status, func.count())
            .where(Document.is_deleted.is_(False))
            .group_by(Document.processing_status)
        )
        model_rows = await session.execute(
            select(ChunkModel.embedding_model, func.count()).group_by(ChunkModel.embedding_model)
        )
        chunks_by_model = {model: count for model, count in model_rows.all()}
        return {
            "configured_model": get_settings().embedding_model_name,
            "documents_by_status": {status.value: count for status, count in status_rows.all()},
            "chunks_embedded_by_model": {m: c for m, c in chunks_by_model.items() if m is not None},
            "chunks_without_embedding": chunks_by_model.get(None, 0),
        }


@app.get("/documents")
async def list_documents(limit: int = 50, offset: int = 0) -> dict:
    session_factory = app.state.session_factory
    async with session_factory() as session:
        stmt = select(Document).order_by(Document.updated_db_at.desc()).limit(limit).offset(offset)
        result = await session.execute(stmt)
        documents = result.scalars().all()
        return {"documents": [_serialize_document(d) for d in documents], "limit": limit, "offset": offset}


@app.get("/documents/{document_id}")
async def get_document(document_id: uuid.UUID) -> dict:
    session_factory = app.state.session_factory
    async with session_factory() as session:
        document = await session.get(Document, document_id)
        if document is None:
            raise HTTPException(status_code=404, detail="document not found")
        return _serialize_document(document)


def _serialize_sync_run(sync_run: SyncRun) -> dict:
    return {
        "sync_id": str(sync_run.id),
        "sync_type": sync_run.sync_type,
        "status": sync_run.status,
        "started_at": sync_run.started_at.isoformat() if sync_run.started_at else None,
        "completed_at": sync_run.completed_at.isoformat() if sync_run.completed_at else None,
        "items_detected": sync_run.items_detected,
        "items_added": sync_run.items_added,
        "items_modified": sync_run.items_modified,
        "items_deleted": sync_run.items_deleted,
        "items_skipped": sync_run.items_skipped,
        "items_failed": sync_run.items_failed,
        "error_summary": sync_run.error_summary,
    }


def _serialize_document(document: Document) -> dict:
    return {
        "id": str(document.id),
        "source_type": document.source_type,
        "sharepoint_item_id": document.sharepoint_item_id,
        "name": document.name,
        "path": document.path,
        "sharepoint_url": document.sharepoint_url,
        "extension": document.extension,
        "size": document.size,
        "content_hash": document.content_hash,
        "processing_status": document.processing_status,
        "processing_error": document.processing_error,
        "is_deleted": document.is_deleted,
        "last_synced_at": document.last_synced_at.isoformat() if document.last_synced_at else None,
    }
