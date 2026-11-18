"""
Sync and embedding job execution, shared by the HTTP endpoints and the
daily scheduler.

Owns each run's lifecycle (spec sections 26/41): a sync_run row is created
up front, the job takes the source's lock (spec section 58), runs, and the
row always ends success/partial/failed -- including when the job crashes or
another run already holds the lock. Every log line emitted while a run
executes carries its sync_id (spec section 46).
"""
from __future__ import annotations

import enum
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chunking.chunker import Chunker
from app.config import Settings
from app.db.models import SyncRun, SyncRunStatus, SyncRunType
from app.embeddings.base import EmbeddingProvider
from app.embeddings.indexer import EmbeddingRunCounters, embed_pending_documents
from app.observability import SYNC_DURATION, SYNC_ITEMS, SYNC_RUNS, current_sync_id
from app.processing.router import ProcessorRouter
from app.sharepoint.client import GraphClient
from app.sharepoint.pages import check_page_capability
from app.sync.locks import EMBEDDING_LOCK, LIBRARY_SYNC_LOCK, PAGE_SYNC_LOCK, LockManager
from app.sync.manager import (
    SyncRunCounters,
    establish_delta_baseline,
    run_delta_sync,
    run_initial_sync,
    run_page_sync,
)
from app.sync.state import SqlAlchemyChunkRepository, SqlAlchemyDocumentRepository, SqlAlchemySyncStateRepository

logger = logging.getLogger("knowledge_agent.jobs")

DOCUMENT_LIBRARY_SOURCE = "document_library"

# Run types that walk the whole drive and therefore count as a reconciliation.
FULL_WALK_TYPES = (SyncRunType.initial, SyncRunType.reconciliation)
LIBRARY_RUN_TYPES = (SyncRunType.initial, SyncRunType.delta, SyncRunType.reconciliation, SyncRunType.manual)
PAGE_RUN_TYPES = (SyncRunType.pages,)

# A run row younger than this may belong to a job that's about to take its
# lock, so startup recovery leaves it alone.
_RECOVERY_GRACE = timedelta(minutes=2)


class LibrarySyncMode(str, enum.Enum):
    auto = "auto"            # delta if a cursor exists, else a full walk
    full = "full"            # full walk regardless (rebuild, spec section 53)
    reconcile = "reconcile"  # same walk, requested as reconciliation (spec section 23)


class SyncAlreadyRunningError(RuntimeError):
    pass


@dataclass
class StartedRun:
    sync_id: uuid.UUID
    sync_type: SyncRunType


def choose_library_sync_type(mode: LibrarySyncMode, has_delta_cursor: bool) -> SyncRunType:
    if not has_delta_cursor:
        return SyncRunType.initial
    if mode == LibrarySyncMode.auto:
        return SyncRunType.delta
    return SyncRunType.reconciliation


def reconciliation_due(last_full_walk: datetime | None, now: datetime, interval_days: int) -> bool:
    if interval_days <= 0:
        return False
    return last_full_walk is None or now - last_full_walk >= timedelta(days=interval_days)


class JobRunner:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        locks: LockManager,
        graph_client_factory: Callable[[], GraphClient],
        router: ProcessorRouter,
        embedding_provider_factory: Callable[[], EmbeddingProvider],
    ):
        self._settings = settings
        self._session_factory = session_factory
        self._locks = locks
        self._graph_client_factory = graph_client_factory
        self._router = router
        self._embedding_provider_factory = embedding_provider_factory

    def _chunker(self) -> Chunker:
        return Chunker(chunk_size=self._settings.chunk_size, chunk_overlap=self._settings.chunk_overlap)

    # --- run creation -------------------------------------------------------

    async def create_library_run(self, mode: LibrarySyncMode) -> StartedRun:
        """
        Creates the sync_run row for a document-library sync and decides its
        type. Raises SyncAlreadyRunningError if one is in progress (on any
        instance) -- a fast, friendly check; the job itself re-checks by
        actually taking the lock, which is what makes it safe.
        """
        if await self._locks.is_held(LIBRARY_SYNC_LOCK):
            raise SyncAlreadyRunningError("a document-library sync is already running")
        async with self._session_factory() as session:
            cursor = await SqlAlchemySyncStateRepository(session).get_delta_link(DOCUMENT_LIBRARY_SOURCE)
            sync_type = choose_library_sync_type(mode, cursor is not None)
            return StartedRun(await self._insert_run(session, sync_type), sync_type)

    async def create_page_run(self) -> StartedRun:
        if await self._locks.is_held(PAGE_SYNC_LOCK):
            raise SyncAlreadyRunningError("a page sync is already running")
        async with self._session_factory() as session:
            return StartedRun(await self._insert_run(session, SyncRunType.pages), SyncRunType.pages)

    @staticmethod
    async def _insert_run(session: AsyncSession, sync_type: SyncRunType) -> uuid.UUID:
        sync_run = SyncRun(id=uuid.uuid4(), sync_type=sync_type, status=SyncRunStatus.running)
        session.add(sync_run)
        await session.commit()
        return sync_run.id

    # --- execution ----------------------------------------------------------

    async def execute_library_sync(self, run: StartedRun, embed_after: bool = True) -> None:
        async def body(session: AsyncSession, client: GraphClient) -> SyncRunCounters:
            doc_repo = SqlAlchemyDocumentRepository(session)
            chunk_repo = SqlAlchemyChunkRepository(session)
            sync_state_repo = SqlAlchemySyncStateRepository(session)
            settings = self._settings

            if run.sync_type == SyncRunType.delta:
                return await run_delta_sync(
                    client=client, drive_id=settings.sharepoint_drive_id, site_id=settings.sharepoint_site_id,
                    doc_repo=doc_repo, sync_state_repo=sync_state_repo, source=DOCUMENT_LIBRARY_SOURCE,
                    router=self._router, chunker=self._chunker(), chunk_repo=chunk_repo,
                )

            counters = await run_initial_sync(
                client=client, drive_id=settings.sharepoint_drive_id, site_id=settings.sharepoint_site_id,
                repo=doc_repo, router=self._router, chunker=self._chunker(), chunk_repo=chunk_repo,
            )
            # Capture a fresh delta cursor so the next run can take the cheap
            # incremental path. A failure here doesn't fail the walk itself --
            # worst case the next auto run repeats the full walk (safe).
            try:
                await establish_delta_baseline(
                    client, settings.sharepoint_drive_id, sync_state_repo, source=DOCUMENT_LIBRARY_SOURCE
                )
                await session.commit()
            except Exception:
                logger.exception("delta_baseline_establishment_failed")
                await session.rollback()
            return counters

        await self._execute(run, LIBRARY_SYNC_LOCK, "a document-library sync is already running", body)
        if embed_after and self._settings.embed_after_sync:
            await self.run_embedding()

    async def execute_page_sync(self, run: StartedRun, embed_after: bool = True) -> None:
        async def body(session: AsyncSession, client: GraphClient) -> SyncRunCounters:
            # Capability check first (spec section 7); never escalate (Rule 6).
            capability = await check_page_capability(client, self._settings.sharepoint_site_id)
            if not capability.available:
                logger.error("page_sync_blocked_by_permissions http_status=%s", capability.http_status)
                raise _RunBlocked(capability.detail)
            return await run_page_sync(
                client, self._settings.sharepoint_site_id, SqlAlchemyDocumentRepository(session),
                chunker=self._chunker(), chunk_repo=SqlAlchemyChunkRepository(session),
            )

        await self._execute(run, PAGE_SYNC_LOCK, "a page sync is already running", body)
        if embed_after and self._settings.embed_after_sync:
            await self.run_embedding()

    async def _execute(self, run: StartedRun, lock_name: str, busy_message: str, body) -> None:
        token = current_sync_id.set(str(run.sync_id))
        started = time.monotonic()
        try:
            async with self._locks.hold(lock_name) as acquired:
                if not acquired:
                    logger.warning("sync_run_skipped_lock_held lock=%s", lock_name)
                    await self._fail_run(run, busy_message, started)
                    return

                logger.info("sync_run_started sync_type=%s", run.sync_type.value)
                async with self._session_factory() as session:
                    client: GraphClient | None = None
                    try:
                        client = self._graph_client_factory()
                        counters = await body(session, client)
                    except _RunBlocked as blocked:
                        await session.rollback()
                        await self._fail_run(run, str(blocked), started)
                        return
                    except Exception as exc:  # noqa: BLE001 -- a run must always be finalized
                        logger.exception("sync_run_crashed sync_type=%s", run.sync_type.value)
                        await session.rollback()
                        await self._fail_run(run, f"{type(exc).__name__}: {exc}", started)
                        return
                    finally:
                        if client is not None:
                            await client.aclose()
                    await self._finish_run(session, run, counters, started)
        finally:
            current_sync_id.reset(token)

    async def _finish_run(
        self, session: AsyncSession, run: StartedRun, counters: SyncRunCounters, started: float
    ) -> None:
        sync_run = await session.get(SyncRun, run.sync_id)
        status = SyncRunStatus.success if not counters.has_failures else SyncRunStatus.partial
        sync_run.items_detected = counters.items_detected
        sync_run.items_added = counters.items_added
        sync_run.items_modified = counters.items_modified
        sync_run.items_deleted = counters.items_deleted
        sync_run.items_skipped = counters.items_skipped
        sync_run.items_failed = counters.items_failed
        sync_run.error_count = len(counters.errors)
        sync_run.error_summary = "; ".join(counters.errors[:20])[:4000] or None
        sync_run.status = status
        sync_run.completed_at = datetime.now(timezone.utc)
        await session.commit()

        duration = time.monotonic() - started
        sync_type = run.sync_type.value
        SYNC_RUNS.labels(sync_type=sync_type, status=status.value).inc()
        SYNC_DURATION.labels(sync_type=sync_type).observe(duration)
        for outcome, count in (
            ("added", counters.items_added), ("modified", counters.items_modified),
            ("deleted", counters.items_deleted), ("skipped", counters.items_skipped),
            ("failed", counters.items_failed),
        ):
            if count:
                SYNC_ITEMS.labels(sync_type=sync_type, outcome=outcome).inc(count)
        logger.info(
            "sync_run_completed sync_type=%s status=%s duration_ms=%d detected=%d added=%d "
            "modified=%d deleted=%d skipped=%d failed=%d",
            sync_type, status.value, duration * 1000, counters.items_detected, counters.items_added,
            counters.items_modified, counters.items_deleted, counters.items_skipped, counters.items_failed,
        )

    async def _fail_run(self, run: StartedRun, reason: str, started: float) -> None:
        # A fresh session: the job's own may be unusable after the failure.
        try:
            async with self._session_factory() as session:
                sync_run = await session.get(SyncRun, run.sync_id)
                if sync_run is not None:
                    sync_run.status = SyncRunStatus.failed
                    sync_run.error_count = max(sync_run.error_count or 0, 1)
                    sync_run.error_summary = reason[:4000]
                    sync_run.completed_at = datetime.now(timezone.utc)
                    await session.commit()
        except Exception:
            logger.exception("failed_to_record_sync_run_failure")
        SYNC_RUNS.labels(sync_type=run.sync_type.value, status=SyncRunStatus.failed.value).inc()
        SYNC_DURATION.labels(sync_type=run.sync_type.value).observe(time.monotonic() - started)

    async def run_embedding(self, reembed_all: bool = False) -> EmbeddingRunCounters | None:
        """
        Never raises: embedding failures are per document (EMBEDDING_FAILED)
        and retried by the next run, so they never affect a sync run's status
        or its delta cursor. Waits for (rather than skips) a concurrent run,
        so documents chunked meanwhile are still picked up afterwards.
        """
        async with self._locks.hold(EMBEDDING_LOCK, wait=True):
            async with self._session_factory() as session:
                try:
                    return await embed_pending_documents(
                        doc_repo=SqlAlchemyDocumentRepository(session),
                        chunk_repo=SqlAlchemyChunkRepository(session),
                        provider=self._embedding_provider_factory(),
                        batch_size=self._settings.embedding_batch_size,
                        reembed_all=reembed_all,
                    )
                except Exception:  # noqa: BLE001 -- background job; log, don't crash the worker
                    logger.exception("embedding_stage_crashed")
                    await session.rollback()
                    return None

    # --- scheduler / startup ------------------------------------------------

    async def last_full_walk_completed_at(self) -> datetime | None:
        async with self._session_factory() as session:
            return await session.scalar(
                select(func.max(SyncRun.completed_at)).where(
                    SyncRun.sync_type.in_(FULL_WALK_TYPES),
                    SyncRun.status.in_((SyncRunStatus.success, SyncRunStatus.partial)),
                )
            )

    async def run_scheduled(self) -> None:
        """The daily job (spec sections 23/58): library sync, pages, then embeddings."""
        now = datetime.now(timezone.utc)
        mode = (
            LibrarySyncMode.reconcile
            if reconciliation_due(
                await self.last_full_walk_completed_at(), now, self._settings.reconciliation_interval_days
            )
            else LibrarySyncMode.auto
        )
        logger.info("scheduled_sync_started library_mode=%s", mode.value)

        try:
            await self.execute_library_sync(await self.create_library_run(mode), embed_after=False)
        except SyncAlreadyRunningError:
            logger.warning("scheduled_library_sync_skipped reason=already_running")

        if self._settings.sync_pages_on_schedule:
            try:
                await self.execute_page_sync(await self.create_page_run(), embed_after=False)
            except SyncAlreadyRunningError:
                logger.warning("scheduled_page_sync_skipped reason=already_running")

        if self._settings.embed_after_sync:
            await self.run_embedding()

    async def recover_interrupted_runs(self) -> int:
        """
        Marks sync_runs left "running" by a process that died as failed, so
        status endpoints don't report a phantom run forever. Only touches a
        source whose lock is free -- if another instance holds it, its
        "running" row is genuinely running.
        """
        recovered = 0
        cutoff = datetime.now(timezone.utc) - _RECOVERY_GRACE
        for lock_name, run_types in ((LIBRARY_SYNC_LOCK, LIBRARY_RUN_TYPES), (PAGE_SYNC_LOCK, PAGE_RUN_TYPES)):
            async with self._locks.hold(lock_name) as acquired:
                if not acquired:
                    continue
                async with self._session_factory() as session:
                    result = await session.execute(
                        update(SyncRun)
                        .where(
                            SyncRun.status == SyncRunStatus.running,
                            SyncRun.sync_type.in_(run_types),
                            SyncRun.started_at < cutoff,
                        )
                        .values(
                            status=SyncRunStatus.failed,
                            error_summary="interrupted: the service stopped before this run finished",
                            completed_at=datetime.now(timezone.utc),
                        )
                    )
                    await session.commit()
                    recovered += result.rowcount or 0
        if recovered:
            logger.warning("recovered_interrupted_sync_runs count=%d", recovered)
        return recovered


class _RunBlocked(Exception):
    """A precondition (e.g. page permissions) stopped the run before it did anything."""
