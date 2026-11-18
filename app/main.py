"""
FastAPI entrypoint: Graph health checks, sync triggers and status, the
embedding stage, operational status/metrics, and (opt-in) debug previews.

Sync and embedding jobs run through app/jobs.py (JobRunner), shared with
the daily scheduler (app/scheduler.py) that the lifespan starts.

POST /chat (Phase 11) serves the SharePoint chat bubble: Entra ID user
tokens (app/auth/entra.py), CORS for the SharePoint origin only, and the
grounded RAG pipeline via app/chat/service.py. Models and the LLM client are
created once in the lifespan, not per request.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, HTTPException, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.auth.entra import ChatAuthError, ChatForbiddenError, ChatUser, EntraTokenValidator
from app.auth.graph_auth import GraphAuth, GraphAuthError
from app.chat.service import ChatService, ConversationNotFoundError
from app.chunking.chunker import Chunker
from app.config import get_settings
from app.db.database import build_engine, build_session_factory
from app.db.models import Chunk as ChunkModel
from app.db.models import Document, SourceType, SyncRun
from app.embeddings.sentence_transformer import SentenceTransformerProvider
from app.jobs import JobRunner, LibrarySyncMode, StartedRun, SyncAlreadyRunningError
from app.llm.azure_openai import AzureOpenAIResponsesClient
from app.llm.base import LLMError
from app.observability import (
    CHAT_REQUESTS,
    CHAT_SECONDS,
    CHUNKS,
    DOCUMENTS,
    LAST_SUCCESSFUL_SYNC,
    SYNC_RUNNING,
    configure_logging,
)
from app.processing.router import UnsupportedExtensionError, build_default_router
from app.retrieval.rerank import CrossEncoderReranker
from app.scheduler import DailyScheduler
from app.sharepoint.client import GraphApiError, GraphClient
from app.sharepoint.drives import DiscoveredFile, check_drive_health, check_site_health, walk_drive
from app.sharepoint.files import download_file
from app.sharepoint.pages import check_page_capability, get_page_content, normalize_page_content
from app.status import collect_status
from app.sync.locks import PostgresAdvisoryLockManager
from app.sync.state import SqlAlchemyDocumentRepository

# One shared router instance -- processors (notably ImageProcessor's
# Tesseract path) have no per-request state worth recreating every call.
_processing_router = build_default_router()
# Same reasoning for the embedding provider, plus it's expensive: the model
# is loaded once (lazily, on first embed) and reused for every run.
_embedding_provider: SentenceTransformerProvider | None = None

logger = logging.getLogger("knowledge_agent.main")


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
            passage_prefix=settings.embedding_passage_prefix,
        )
    return _embedding_provider


def _build_reranker(settings) -> CrossEncoderReranker | None:
    if not settings.rerank_enabled:
        return None
    return CrossEncoderReranker(
        settings.reranker_model_name, device=settings.embedding_device, batch_size=settings.reranker_batch_size
    )


def _build_llm_client(settings) -> AzureOpenAIResponsesClient | None:
    if settings.azure_openai_api_key is None:
        return None
    return AzureOpenAIResponsesClient(
        endpoint=settings.azure_openai_endpoint,
        api_key=settings.azure_openai_api_key.get_secret_value(),
        deployment=settings.azure_openai_deployment,
        temperature=settings.llm_temperature,
        max_output_tokens=settings.llm_max_output_tokens,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )


async def _warm_models(embedding_provider, reranker) -> None:
    # Loading torch + the models takes seconds; do it now, in the
    # background, so the first chat request doesn't pay for it. /health
    # stays responsive meanwhile.
    started = time.perf_counter()
    try:
        await embedding_provider.embed_query("warm up")
        if reranker is not None:
            await reranker.score("warm up", ["warm up"])
        logger.info("chat_models_warmed seconds=%.1f", time.perf_counter() - started)
    except Exception:  # noqa: BLE001 -- warm-up is an optimisation; requests load lazily anyway
        logger.exception("chat_model_warmup_failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    if settings.api_key is None:
        logger.warning("api_key_not_configured -- every endpoint is unauthenticated; set API_KEY in production")

    engine = build_engine(settings)
    app.state.session_factory = build_session_factory(engine)
    app.state.runner = JobRunner(
        settings=settings,
        session_factory=app.state.session_factory,
        locks=PostgresAdvisoryLockManager(engine),
        graph_client_factory=_build_graph_client,
        router=_processing_router,
        embedding_provider_factory=_get_embedding_provider,
    )
    await app.state.runner.recover_interrupted_runs()

    llm_client = _build_llm_client(settings)
    reranker = _build_reranker(settings)
    warmup: asyncio.Task | None = None
    if llm_client is None:
        app.state.chat_service = None
        logger.warning("chat_disabled -- AZURE_OPENAI_API_KEY is not set; POST /chat returns 503")
    else:
        app.state.chat_service = ChatService(
            settings=settings,
            session_factory=app.state.session_factory,
            embedding_provider=_get_embedding_provider(),
            reranker=reranker,
            llm=llm_client,
        )
        if settings.chat_warmup_models:
            warmup = asyncio.create_task(_warm_models(_get_embedding_provider(), reranker))
    if settings.chat_audiences:
        app.state.chat_token_validator = EntraTokenValidator(
            settings.tenant_id, settings.chat_audiences, settings.chat_required_scope,
            required_role=settings.chat_required_role,
        )
    else:
        logger.warning("chat_auth_audience_not_set -- POST /chat uses API_KEY auth with a shared identity (dev mode)")

    scheduler: DailyScheduler | None = None
    if settings.sync_schedule_enabled:
        scheduler = DailyScheduler(app.state.runner, settings.sync_schedule_time, settings.sync_schedule_timezone)
        scheduler.start()
    try:
        yield
    finally:
        if scheduler is not None:
            await scheduler.stop()
        if warmup is not None and not warmup.done():
            warmup.cancel()
        if llm_client is not None:
            await llm_client.aclose()
        await engine.dispose()


app = FastAPI(title="Auximo Knowledge Agent", version="1.1.0", lifespan=lifespan)


class _SettingsCORSMiddleware:
    """
    CORSMiddleware configured from settings on first request rather than at
    import time (settings load lazily; tests swap them). Exact origins only
    (validated in Settings); no cookies are involved -- the chat bubble
    sends an Authorization header -- so credentials stay disabled.
    """

    def __init__(self, app):
        self.app = app
        self._cors: CORSMiddleware | None = None

    async def __call__(self, scope, receive, send):
        if self._cors is None:
            self._cors = CORSMiddleware(
                self.app,
                allow_origins=get_settings().cors_origins,
                allow_methods=["GET", "POST", "OPTIONS"],
                allow_headers=["Content-Type", "Authorization", "X-API-Key"],
                allow_credentials=False,
                # Lets the SharePoint page reach a backend on localhost during
                # SPFx development (Chrome Private Network Access).
                allow_private_network=True,
                max_age=600,
            )
        await self._cors(scope, receive, send)


app.add_middleware(_SettingsCORSMiddleware)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer = HTTPBearer(auto_error=False)


async def require_api_key(
    api_key: str | None = Security(_api_key_header),
    bearer: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    """
    Accepts the key as "X-API-Key: <key>" or "Authorization: Bearer <key>"
    (the latter is what Prometheus' scrape config can send). No-op when
    API_KEY isn't configured.
    """
    expected = get_settings().api_key
    if expected is None:
        return
    supplied = api_key or (bearer.credentials if bearer else None)
    if supplied is None or not hmac.compare_digest(
        supplied.encode("utf-8"), expected.get_secret_value().encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="missing or invalid API key")


async def require_debug_enabled() -> None:
    # 404 rather than 403: when disabled, these endpoints simply don't exist.
    if not get_settings().enable_debug_endpoints:
        raise HTTPException(status_code=404, detail="Not Found")


def _chat_token_validator() -> EntraTokenValidator:
    validator = getattr(app.state, "chat_token_validator", None)
    if validator is None:
        settings = get_settings()
        validator = EntraTokenValidator(
            settings.tenant_id, settings.chat_audiences, settings.chat_required_scope,
            required_role=settings.chat_required_role,
        )
        app.state.chat_token_validator = validator
    return validator


async def require_chat_user(
    api_key: str | None = Security(_api_key_header),
    bearer: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> ChatUser:
    """
    Production (CHAT_AUTH_AUDIENCE set): a valid Entra ID user token is
    required, and the user's identity comes from it; with CHAT_REQUIRED_ROLE
    set, the user must also hold that app role (else 403). Development (unset):
    falls back to API_KEY, with one shared identity for every caller.
    """
    if get_settings().chat_audiences:
        if bearer is None:
            raise HTTPException(status_code=401, detail="missing bearer token", headers={"WWW-Authenticate": "Bearer"})
        try:
            return await _chat_token_validator().validate(bearer.credentials)
        except ChatAuthError as exc:
            # The reason is useful to operators, useless (or helpful) to attackers.
            logger.warning("chat_token_rejected reason=%s", exc)
            raise HTTPException(
                status_code=401, detail="invalid or expired token", headers={"WWW-Authenticate": "Bearer"}
            ) from exc
        except ChatForbiddenError as exc:
            # oid is a pseudonymous id -- enough for an admin to look the user up.
            logger.warning("chat_access_denied reason=%s", exc)
            raise HTTPException(status_code=403, detail="you do not have access to the Knowledge Hub Assistant") from exc
    await require_api_key(api_key, bearer)
    return ChatUser(user_id="local-dev", name="Local development")


protected = APIRouter(dependencies=[Depends(require_api_key)])
# /chat authenticates users itself (Entra tokens), so it isn't on `protected`.
chat_router = APIRouter()
debug_router = APIRouter(dependencies=[Depends(require_api_key), Depends(require_debug_enabled)])


def _runner() -> JobRunner:
    return app.state.runner


@app.get("/health")
async def health() -> dict:
    """Liveness check -- unauthenticated; does not touch Graph or the network."""
    return {"status": "ok"}


@protected.get("/health/graph")
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


@debug_router.get("/discovery/preview")
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


@protected.get("/health/pages")
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


@debug_router.get("/debug/pages/{page_id}/preview", response_class=PlainTextResponse)
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


@debug_router.get("/debug/files/{item_id}/preview", response_class=PlainTextResponse)
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


@debug_router.get("/debug/files/{item_id}/chunks")
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


@debug_router.get("/debug/pages/{page_id}/chunks")
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


def _started_response(run: StartedRun) -> dict:
    return {"sync_id": str(run.sync_id), "status": "started", "sync_type": run.sync_type.value}


async def _start_library_sync(background_tasks: BackgroundTasks, mode: LibrarySyncMode) -> dict:
    try:
        run = await _runner().create_library_run(mode)
    except SyncAlreadyRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    background_tasks.add_task(_runner().execute_library_sync, run)
    return _started_response(run)


@protected.post("/sync/pages")
async def trigger_page_sync(background_tasks: BackgroundTasks) -> dict:
    """
    Deliberately a separate endpoint from POST /sync (spec section 7):
    page sync depends on a permission that may not be granted, and must
    never silently piggyback on, or block, document-library sync. Call
    GET /health/pages first if you just want to check capability without
    triggering a run.
    """
    try:
        run = await _runner().create_page_run()
    except SyncAlreadyRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    background_tasks.add_task(_runner().execute_page_sync, run)
    return _started_response(run)


@protected.post("/sync/reconcile")
async def trigger_reconciliation(background_tasks: BackgroundTasks) -> dict:
    """
    Full reconciliation (spec sections 23/59): walk the whole drive, repair
    anything delta missed, mark tracked files SharePoint no longer has as
    deleted, and re-establish the delta cursor. Same as POST /sync?mode=full.
    """
    return await _start_library_sync(background_tasks, LibrarySyncMode.reconcile)


@protected.post("/sync")
async def trigger_sync(background_tasks: BackgroundTasks, mode: str = "auto") -> dict:
    """
    Creates a sync_run row immediately and returns sync_id without waiting
    for the sync to finish (spec section 40/41). 409 if a document-library
    sync is already running (spec section 58).

    mode=auto (default), per spec section 64's daily-sync diagram: if no
    delta cursor exists yet, run the full recursive walk and establish a
    cursor at the end; once a cursor exists, take the incremental delta path.

    mode=full (spec section 53): always run the full walk (a reconciliation)
    and re-establish the cursor. Rebuild/repair path -- safe to run any time,
    since unchanged content is detected by hash and skipped.
    """
    if mode not in ("auto", "full"):
        raise HTTPException(status_code=400, detail="mode must be 'auto' or 'full'")
    return await _start_library_sync(background_tasks, LibrarySyncMode(mode))


@protected.get("/sync/latest")
async def get_latest_sync() -> dict:
    session_factory = app.state.session_factory
    async with session_factory() as session:
        stmt = select(SyncRun).order_by(SyncRun.started_at.desc()).limit(1)
        result = await session.execute(stmt)
        sync_run = result.scalar_one_or_none()
        if sync_run is None:
            raise HTTPException(status_code=404, detail="no sync runs yet")
        return _serialize_sync_run(sync_run)


@protected.get("/sync/{sync_id}")
async def get_sync_status(sync_id: uuid.UUID) -> dict:
    session_factory = app.state.session_factory
    async with session_factory() as session:
        sync_run = await session.get(SyncRun, sync_id)
        if sync_run is None:
            raise HTTPException(status_code=404, detail="sync_run not found")
        return _serialize_sync_run(sync_run)


@protected.post("/embeddings/run")
async def trigger_embedding_run(background_tasks: BackgroundTasks, reembed: str = "pending") -> dict:
    """
    reembed=pending (default): embed everything CHUNKED / EMBEDDING_FAILED /
    embedded by a different model. reembed=all: re-embed every indexed
    document too (spec section 35's full re-embedding path).
    """
    if reembed not in ("pending", "all"):
        raise HTTPException(status_code=400, detail="reembed must be 'pending' or 'all'")
    background_tasks.add_task(_runner().run_embedding, reembed == "all")
    return {"status": "started", "reembed": reembed}


@protected.get("/embeddings/status")
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


@protected.get("/status")
async def get_status() -> dict:
    """Operational overview (spec section 47): syncs, counts, failures, last error."""
    async with app.state.session_factory() as session:
        status = await collect_status(session)
    return {
        "last_successful_sync": {
            source: at.isoformat() if at else None for source, at in status.last_successful_sync.items()
        },
        "running_syncs": status.running_syncs,
        "delta_cursor_state": status.delta_cursor_state,
        "documents": {
            "by_status": status.documents_by_status,
            "total": sum(status.documents_by_status.values()),
            "deleted": status.documents_deleted,
        },
        "chunks": {"total": status.chunks_total, "embedded": status.chunks_embedded},
        "failed_documents": {"total": status.failed_documents_total, "recent": status.failed_documents},
        "last_error": status.last_error,
        "scheduler": {
            "enabled": get_settings().sync_schedule_enabled,
            "time": get_settings().sync_schedule_time,
            "timezone": get_settings().sync_schedule_timezone or "server local",
        },
    }


@protected.get("/metrics")
async def metrics() -> Response:
    """Prometheus exposition. Database-backed gauges are refreshed on every scrape."""
    async with app.state.session_factory() as session:
        status = await collect_status(session, failed_limit=0)
    DOCUMENTS.clear()
    for doc_status, count in status.documents_by_status.items():
        DOCUMENTS.labels(status=doc_status).set(count)
    CHUNKS.labels(embedded="true").set(status.chunks_embedded)
    CHUNKS.labels(embedded="false").set(status.chunks_total - status.chunks_embedded)
    for source, at in status.last_successful_sync.items():
        if at is not None:
            LAST_SUCCESSFUL_SYNC.labels(source=source).set(at.timestamp())
    SYNC_RUNNING.clear()
    library_types = {"initial", "delta", "reconciliation", "manual"}
    running = [r["sync_type"] for r in status.running_syncs]
    SYNC_RUNNING.labels(source="document_library").set(sum(t in library_types for t in running))
    SYNC_RUNNING.labels(source="site_pages").set(sum(t == "pages" for t in running))
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@protected.get("/documents")
async def list_documents(limit: int = 50, offset: int = 0) -> dict:
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    session_factory = app.state.session_factory
    async with session_factory() as session:
        stmt = select(Document).order_by(Document.updated_db_at.desc()).limit(limit).offset(offset)
        result = await session.execute(stmt)
        documents = result.scalars().all()
        return {"documents": [_serialize_document(d) for d in documents], "limit": limit, "offset": offset}


@protected.get("/documents/{document_id}")
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


class ChatRequest(BaseModel):
    # SHAREPOINT_AGENT_INTEGRATION.md section 4. "user" and "page_url" are
    # accepted for compatibility with the reference UI but never trusted:
    # identity comes from the token.
    model_config = ConfigDict(extra="ignore")

    message: str = Field(min_length=1)
    conversation_id: uuid.UUID | None = None
    user: str | None = None
    page_url: str | None = None


class ChatSourceOut(BaseModel):
    number: int
    title: str
    url: str | None
    sections: list[str]
    source_type: str
    cited: bool


class ChatResponse(BaseModel):
    reply: str
    conversation_id: uuid.UUID
    sources: list[ChatSourceOut]
    # Set when the question named an 8-digit project number.
    project_numbers: list[str] = []
    project_scope_fell_back: bool = False


@chat_router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, user: ChatUser = Depends(require_chat_user)) -> ChatResponse:
    settings = get_settings()
    message = body.message.strip()
    if not message:
        CHAT_REQUESTS.labels(outcome="invalid").inc()
        raise HTTPException(status_code=422, detail="message must not be empty")
    if len(message) > settings.chat_max_message_chars:
        CHAT_REQUESTS.labels(outcome="invalid").inc()
        raise HTTPException(
            status_code=422, detail=f"message is longer than {settings.chat_max_message_chars} characters"
        )

    service: ChatService | None = getattr(app.state, "chat_service", None)
    if service is None:
        CHAT_REQUESTS.labels(outcome="unavailable").inc()
        raise HTTPException(status_code=503, detail="chat is not configured on this server")

    started = time.perf_counter()
    try:
        reply = await service.handle(user, message, body.conversation_id)
    except ConversationNotFoundError as exc:
        CHAT_REQUESTS.labels(outcome="conversation_not_found").inc()
        raise HTTPException(status_code=404, detail="conversation not found") from exc
    except LLMError as exc:
        CHAT_REQUESTS.labels(outcome="llm_error").inc()
        logger.error("chat_llm_failed status=%s", exc.status_code)
        raise HTTPException(status_code=502, detail="the language model is unavailable, please try again") from exc

    CHAT_REQUESTS.labels(outcome="answered" if reply.llm_called else "no_context").inc()
    CHAT_SECONDS.observe(time.perf_counter() - started)
    return ChatResponse(
        reply=reply.reply,
        conversation_id=reply.conversation_id,
        sources=[ChatSourceOut(**s) for s in reply.sources],
        project_numbers=reply.project_numbers,
        project_scope_fell_back=reply.project_scope_fell_back,
    )


# Registered last: routes match in declaration order, and including a
# router appends its routes to the app in their own declaration order.
app.include_router(protected)
app.include_router(chat_router)
app.include_router(debug_router)
