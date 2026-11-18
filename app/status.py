"""
Operational status read from the database (spec section 47's minimum:
last successful sync, current sync status, document and chunk counts,
failed documents, last error). Backs GET /status and the database gauges
on GET /metrics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Chunk, Document, ProcessingStatus, SyncRun, SyncRunStatus, SyncState
from app.jobs import LIBRARY_RUN_TYPES, PAGE_RUN_TYPES

FAILED_STATUSES = (
    ProcessingStatus.download_failed,
    ProcessingStatus.extraction_failed,
    ProcessingStatus.chunking_failed,
    ProcessingStatus.embedding_failed,
)
SOURCES = {"document_library": LIBRARY_RUN_TYPES, "site_pages": PAGE_RUN_TYPES}


@dataclass
class SystemStatus:
    last_successful_sync: dict[str, datetime | None] = field(default_factory=dict)
    running_syncs: list[dict] = field(default_factory=list)
    delta_cursor_state: dict[str, dict] = field(default_factory=dict)
    documents_by_status: dict[str, int] = field(default_factory=dict)
    documents_deleted: int = 0
    chunks_total: int = 0
    chunks_embedded: int = 0
    failed_documents: list[dict] = field(default_factory=list)
    failed_documents_total: int = 0
    last_error: dict | None = None


async def collect_status(session: AsyncSession, failed_limit: int = 20) -> SystemStatus:
    status = SystemStatus()

    for source, run_types in SOURCES.items():
        status.last_successful_sync[source] = await session.scalar(
            select(func.max(SyncRun.completed_at)).where(
                SyncRun.sync_type.in_(run_types),
                SyncRun.status.in_((SyncRunStatus.success, SyncRunStatus.partial)),
            )
        )

    running = await session.execute(
        select(SyncRun).where(SyncRun.status == SyncRunStatus.running).order_by(SyncRun.started_at)
    )
    status.running_syncs = [
        {"sync_id": str(r.id), "sync_type": r.sync_type.value, "started_at": r.started_at.isoformat()}
        for r in running.scalars().all()
    ]

    for row in (await session.execute(select(SyncState))).scalars().all():
        status.delta_cursor_state[row.source] = {
            "status": row.status,
            "has_cursor": row.delta_link is not None,
            "last_successful_sync": row.last_successful_sync.isoformat() if row.last_successful_sync else None,
            "last_attempted_sync": row.last_attempted_sync.isoformat() if row.last_attempted_sync else None,
        }

    by_status = await session.execute(
        select(Document.processing_status, func.count())
        .where(Document.is_deleted.is_(False))
        .group_by(Document.processing_status)
    )
    status.documents_by_status = {s.value: n for s, n in by_status.all()}
    status.documents_deleted = await session.scalar(
        select(func.count()).select_from(Document).where(Document.is_deleted.is_(True))
    ) or 0

    live_chunks = (
        select(func.count())
        .select_from(Chunk)
        .join(Document, Document.id == Chunk.document_id)
        .where(Document.is_deleted.is_(False))
    )
    status.chunks_total = await session.scalar(live_chunks) or 0
    status.chunks_embedded = await session.scalar(live_chunks.where(Chunk.embedding.is_not(None))) or 0

    failed_filter = (Document.is_deleted.is_(False), Document.processing_status.in_(FAILED_STATUSES))
    status.failed_documents_total = await session.scalar(
        select(func.count()).select_from(Document).where(*failed_filter)
    ) or 0
    failed = await session.execute(
        select(Document).where(*failed_filter).order_by(Document.updated_db_at.desc()).limit(failed_limit)
    )
    status.failed_documents = [
        {
            "id": str(d.id),
            "name": d.name,
            "source_type": d.source_type.value,
            "processing_status": d.processing_status.value,
            "processing_error": (d.processing_error or "")[:500] or None,
            "retry_count": d.retry_count,
        }
        for d in failed.scalars().all()
    ]

    last_error_run = (await session.execute(
        select(SyncRun)
        .where(SyncRun.error_summary.is_not(None), SyncRun.error_summary != "")
        .order_by(SyncRun.started_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if last_error_run is not None:
        status.last_error = {
            "sync_id": str(last_error_run.id),
            "sync_type": last_error_run.sync_type.value,
            "status": last_error_run.status.value,
            "at": (last_error_run.completed_at or last_error_run.started_at).isoformat(),
            "error_summary": last_error_run.error_summary,
        }

    return status
