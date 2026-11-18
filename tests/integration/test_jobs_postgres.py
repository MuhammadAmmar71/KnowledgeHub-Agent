"""
Job locking, run lifecycle, reconciliation and status against real Postgres:
the behavior spec sections 23, 47 and 58 care about lives in SQL (advisory
locks, the sync_runs rows), which the in-memory doubles can't exercise.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.db.database import build_session_factory
from app.db.models import Document, ProcessingStatus, SyncRun, SyncRunStatus, SyncRunType
from app.jobs import JobRunner, LibrarySyncMode, StartedRun, SyncAlreadyRunningError
from app.processing.router import build_default_router
from app.sharepoint.client import GraphClient
from app.status import collect_status
from app.sync.locks import LIBRARY_SYNC_LOCK, PostgresAdvisoryLockManager
from tests.conftest import FakeEmbeddingProvider, FakeGraphAuth


class FakeDrive:
    """A tiny SharePoint document library served through httpx.MockTransport."""

    def __init__(self, files: dict[str, tuple[str, bytes]]):
        self.files = files  # item_id -> (name, content)
        self.delta_calls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/drives/drive1/root/children"):
            return httpx.Response(200, json={"value": [
                {"id": item_id, "name": name, "file": {}, "size": len(content), "webUrl": f"https://sp/{name}"}
                for item_id, (name, content) in self.files.items()
            ]})
        if url.endswith("/drives/drive1/root/delta"):
            self.delta_calls += 1
            return httpx.Response(200, json={"value": [], "@odata.deltaLink": "https://graph/_cursor"})
        for item_id, (_, content) in self.files.items():
            if f"/items/{item_id}/content" in url:
                return httpx.Response(200, content=content)
        return httpx.Response(404)


def _runner(settings, engine, drive: FakeDrive, locks=None) -> JobRunner:
    settings.sharepoint_drive_id = "drive1"
    return JobRunner(
        settings=settings,
        session_factory=build_session_factory(engine),
        locks=locks or PostgresAdvisoryLockManager(engine),
        graph_client_factory=lambda: GraphClient(FakeGraphAuth(), settings, transport=httpx.MockTransport(drive.handler)),
        router=build_default_router(),
        embedding_provider_factory=lambda: FakeEmbeddingProvider(),
    )


async def _run_row(engine, sync_id: uuid.UUID) -> SyncRun:
    async with build_session_factory(engine)() as session:
        return await session.get(SyncRun, sync_id)


async def test_advisory_lock_is_exclusive_across_connections(engine):
    a, b = PostgresAdvisoryLockManager(engine), PostgresAdvisoryLockManager(engine)
    async with a.hold("test-lock") as got_a:
        assert got_a is True
        assert await b.is_held("test-lock") is True
        async with b.hold("test-lock") as got_b:
            assert got_b is False
    assert await b.is_held("test-lock") is False


async def test_waiting_lock_acquires_after_release(engine):
    a, b = PostgresAdvisoryLockManager(engine), PostgresAdvisoryLockManager(engine)
    order = []

    async def waiter():
        async with b.hold("test-lock", wait=True):
            order.append("waiter")

    async with a.hold("test-lock"):
        task = asyncio.create_task(waiter())
        await asyncio.sleep(0.2)
        order.append("holder")
    await asyncio.wait_for(task, timeout=5)
    assert order == ["holder", "waiter"]


async def test_full_pipeline_then_reconciliation_removes_stale_file(settings, engine):
    drive = FakeDrive({"i1": ("guide.md", b"# Guide\nHow to deploy."), "i2": ("old.md", b"# Old\nObsolete.")})
    runner = _runner(settings, engine, drive)

    first = await runner.create_library_run(LibrarySyncMode.auto)
    assert first.sync_type == SyncRunType.initial  # no cursor yet
    await runner.execute_library_sync(first)

    row = await _run_row(engine, first.sync_id)
    assert row.status == SyncRunStatus.success
    assert row.items_added == 2
    assert drive.delta_calls == 1  # baseline cursor established

    async with build_session_factory(engine)() as session:
        status = await collect_status(session)
    assert status.documents_by_status == {"INDEXED": 2}
    assert status.chunks_total == status.chunks_embedded > 0
    assert status.last_successful_sync["document_library"] is not None
    assert status.running_syncs == []

    # i2 is deleted in SharePoint without the delta feed ever saying so.
    del drive.files["i2"]
    second = await runner.create_library_run(LibrarySyncMode.reconcile)
    assert second.sync_type == SyncRunType.reconciliation
    await runner.execute_library_sync(second)

    row = await _run_row(engine, second.sync_id)
    assert row.status == SyncRunStatus.success
    assert row.items_deleted == 1
    assert row.items_added == row.items_modified == 0  # unchanged content isn't reprocessed
    async with build_session_factory(engine)() as session:
        deleted = await session.scalar(select(Document.is_deleted).where(Document.sharepoint_item_id == "i2"))
        status = await collect_status(session)
    assert deleted is True
    assert status.documents_by_status == {"INDEXED": 1}
    assert status.documents_deleted == 1


async def test_sync_is_refused_while_another_instance_holds_the_lock(settings, engine):
    drive = FakeDrive({"i1": ("a.md", b"# A\ntext")})
    runner = _runner(settings, engine, drive)
    other_instance = PostgresAdvisoryLockManager(engine)

    async with other_instance.hold(LIBRARY_SYNC_LOCK):
        with pytest.raises(SyncAlreadyRunningError):
            await runner.create_library_run(LibrarySyncMode.auto)

    # Lost the race: the row was created, but the lock was taken before the job started.
    run = await runner.create_library_run(LibrarySyncMode.auto)
    async with other_instance.hold(LIBRARY_SYNC_LOCK):
        await runner.execute_library_sync(run, embed_after=False)

    row = await _run_row(engine, run.sync_id)
    assert row.status == SyncRunStatus.failed
    assert "already running" in row.error_summary
    async with build_session_factory(engine)() as session:
        assert await session.scalar(select(Document.id).limit(1)) is None  # nothing was synced


async def test_crashed_job_is_finalized_as_failed(settings, engine):
    drive = FakeDrive({})
    runner = _runner(settings, engine, drive)

    def exploding_factory():
        raise RuntimeError("graph client construction failed")

    run = await runner.create_library_run(LibrarySyncMode.auto)
    runner._graph_client_factory = exploding_factory
    await runner.execute_library_sync(run, embed_after=False)
    row = await _run_row(engine, run.sync_id)
    assert row.status == SyncRunStatus.failed
    assert "graph client construction failed" in row.error_summary

    # Graph failing mid-walk is recorded the same way.
    runner._graph_client_factory = lambda: GraphClient(
        FakeGraphAuth(), settings, transport=httpx.MockTransport(lambda r: httpx.Response(500))
    )
    run = await runner.create_library_run(LibrarySyncMode.auto)
    await runner.execute_library_sync(run, embed_after=False)
    row = await _run_row(engine, run.sync_id)
    assert row.status == SyncRunStatus.failed
    assert row.completed_at is not None
    assert "GraphApiError" in row.error_summary


async def test_recover_interrupted_runs_only_touches_stale_unlocked_runs(settings, engine):
    runner = _runner(settings, engine, FakeDrive({}))
    session_factory = build_session_factory(engine)
    old, fresh, pages = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    async with session_factory() as session:
        session.add_all([
            SyncRun(id=old, sync_type=SyncRunType.delta, status=SyncRunStatus.running, started_at=hour_ago),
            SyncRun(id=fresh, sync_type=SyncRunType.delta, status=SyncRunStatus.running),
            SyncRun(id=pages, sync_type=SyncRunType.pages, status=SyncRunStatus.running, started_at=hour_ago),
        ])
        await session.commit()

    # Another instance is genuinely running a library sync: leave those rows alone.
    async with PostgresAdvisoryLockManager(engine).hold(LIBRARY_SYNC_LOCK):
        assert await runner.recover_interrupted_runs() == 1  # only the pages run
    assert (await _run_row(engine, old)).status == SyncRunStatus.running

    assert await runner.recover_interrupted_runs() == 1  # now the stale library run
    assert (await _run_row(engine, old)).status == SyncRunStatus.failed
    assert "interrupted" in (await _run_row(engine, old)).error_summary
    assert (await _run_row(engine, fresh)).status == SyncRunStatus.running  # within the grace period
    assert (await _run_row(engine, pages)).status == SyncRunStatus.failed


async def test_status_reports_failed_documents_and_last_error(settings, engine):
    session_factory = build_session_factory(engine)
    async with session_factory() as session:
        session.add(Document(
            id=uuid.uuid4(), source_type="file", sharepoint_item_id="bad", name="bad.pdf", extension=".pdf",
            processing_status=ProcessingStatus.extraction_failed, processing_error="broken xref table",
            retry_count=2, is_deleted=False,
        ))
        session.add(SyncRun(
            id=uuid.uuid4(), sync_type=SyncRunType.delta, status=SyncRunStatus.partial,
            error_summary="bad.pdf (bad)", completed_at=datetime.now(timezone.utc),
        ))
        await session.commit()
        status = await collect_status(session)

    assert status.failed_documents_total == 1
    assert status.failed_documents[0]["processing_error"] == "broken xref table"
    assert status.last_error["error_summary"] == "bad.pdf (bad)"
    assert status.documents_by_status == {"EXTRACTION_FAILED": 1}
