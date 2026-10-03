"""
Document state persistence, kept behind a narrow repository interface so
sync orchestration (manager.py) never talks SQL directly -- Rule 10 says
Graph sync logic must not be coupled to extraction logic, and the same
separation principle applies here: sync logic should not be coupled to a
specific persistence engine either.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.chunking.chunker import Chunk
from app.db.models import Chunk as ChunkModel
from app.db.models import Document, ProcessingStatus, SourceType, SyncState


@dataclass
class DocumentUpsert:
    source_type: SourceType
    sharepoint_item_id: str
    drive_id: str | None
    site_id: str | None
    name: str
    extension: str | None
    path: str | None
    sharepoint_url: str | None
    parent_sharepoint_item_id: str | None
    modified_at: datetime | None
    etag: str | None
    size: int | None
    content_hash: str | None
    processing_status: ProcessingStatus = ProcessingStatus.pending
    processing_error: str | None = None
    retry_count: int = 0


class DocumentRepository(Protocol):
    async def get_by_sharepoint_item_id(
        self, source_type: SourceType, sharepoint_item_id: str
    ) -> Document | None: ...

    async def upsert(self, data: DocumentUpsert) -> Document: ...

    async def touch_metadata_only(self, document: Document, seen_at: datetime) -> None: ...

    async def mark_deleted(self, source_type: SourceType, sharepoint_item_id: str) -> bool: ...

    async def mark_missing_deleted(self, source_type: SourceType, seen_item_ids: set[str]) -> int:
        """
        For sources with no delta/change feed (pages, spec section 31): marks
        every non-deleted document of this source_type whose
        sharepoint_item_id is NOT in seen_item_ids as deleted. Returns the
        count marked. This is a full-listing diff, not an incremental
        signal -- the only correct way to detect a page deletion given
        Graph has no documented sitePage delta endpoint.
        """
        ...

    async def mark_processing_status(
        self, document_id: uuid.UUID, status: ProcessingStatus, error: str | None = None
    ) -> None:
        """
        Updates a document's pipeline status after a stage completes or
        fails, independent of the upsert that created/updated it -- used by
        the chunking step (spec section 56's state machine) once extraction
        and chunk persistence either succeed (-> CHUNKED) or fail
        (-> EXTRACTION_FAILED / CHUNKING_FAILED).
        """
        ...

    async def rollback(self) -> None:
        """
        Recover the underlying session/transaction after a failed write, so
        one bad item in a batch can't poison every write after it in the
        same sync run (spec section 43: continue processing other items).
        """
        ...

    async def commit(self) -> None:
        """
        Make work so far durable. The embedding stage commits after each
        document so a later rollback (one failing document) can't discard
        documents that already finished.
        """
        ...

    async def get_by_id(self, document_id: uuid.UUID) -> Document | None: ...

    async def list_by_processing_status(self, statuses: set[ProcessingStatus]) -> list[Document]:
        """Non-deleted documents currently in any of the given statuses."""
        ...


class SqlAlchemyDocumentRepository:
    """Production implementation, backed by PostgreSQL via SQLAlchemy async session."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_by_sharepoint_item_id(
        self, source_type: SourceType, sharepoint_item_id: str
    ) -> Document | None:
        stmt = select(Document).where(
            Document.source_type == source_type,
            Document.sharepoint_item_id == sharepoint_item_id,
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def upsert(self, data: DocumentUpsert) -> Document:
        existing = await self.get_by_sharepoint_item_id(data.source_type, data.sharepoint_item_id)
        now = datetime.now(timezone.utc)

        if existing is None:
            doc = Document(
                id=uuid.uuid4(),
                source_type=data.source_type,
                sharepoint_item_id=data.sharepoint_item_id,
                drive_id=data.drive_id,
                site_id=data.site_id,
                name=data.name,
                extension=data.extension,
                path=data.path,
                sharepoint_url=data.sharepoint_url,
                parent_sharepoint_item_id=data.parent_sharepoint_item_id,
                modified_at=data.modified_at,
                etag=data.etag,
                size=data.size,
                content_hash=data.content_hash,
                processing_status=data.processing_status,
                processing_error=data.processing_error,
                retry_count=data.retry_count,
                is_deleted=False,
                last_seen_at=now,
                last_synced_at=now,
                last_processed_at=now,
            )
            self._session.add(doc)
            await self._session.flush()
            return doc

        existing.name = data.name
        existing.path = data.path
        existing.sharepoint_url = data.sharepoint_url
        existing.parent_sharepoint_item_id = data.parent_sharepoint_item_id
        existing.modified_at = data.modified_at
        existing.etag = data.etag
        existing.size = data.size
        existing.content_hash = data.content_hash
        existing.processing_status = data.processing_status
        existing.processing_error = data.processing_error
        existing.retry_count = data.retry_count
        existing.is_deleted = False
        existing.last_seen_at = now
        existing.last_synced_at = now
        existing.last_processed_at = now
        await self._session.flush()
        return existing

    async def touch_metadata_only(self, document: Document, seen_at: datetime) -> None:
        document.last_seen_at = seen_at
        document.last_synced_at = seen_at
        await self._session.flush()

    async def mark_deleted(self, source_type: SourceType, sharepoint_item_id: str) -> bool:
        doc = await self.get_by_sharepoint_item_id(source_type, sharepoint_item_id)
        if doc is None:
            return False
        doc.is_deleted = True
        await self._session.flush()
        return True

    async def mark_missing_deleted(self, source_type: SourceType, seen_item_ids: set[str]) -> int:
        stmt = select(Document).where(
            Document.source_type == source_type,
            Document.is_deleted.is_(False),
        )
        result = await self._session.execute(stmt)
        count = 0
        for doc in result.scalars().all():
            if doc.sharepoint_item_id not in seen_item_ids:
                doc.is_deleted = True
                count += 1
        if count:
            await self._session.flush()
        return count

    async def mark_processing_status(
        self, document_id: uuid.UUID, status: ProcessingStatus, error: str | None = None
    ) -> None:
        doc = await self._session.get(Document, document_id)
        if doc is None:
            return
        doc.processing_status = status
        doc.processing_error = error
        await self._session.flush()

    async def rollback(self) -> None:
        await self._session.rollback()

    async def commit(self) -> None:
        await self._session.commit()

    async def get_by_id(self, document_id: uuid.UUID) -> Document | None:
        return await self._session.get(Document, document_id)

    async def list_by_processing_status(self, statuses: set[ProcessingStatus]) -> list[Document]:
        stmt = select(Document).where(
            Document.processing_status.in_(statuses),
            Document.is_deleted.is_(False),
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())


class InMemoryDocumentRepository:
    """
    Test double -- no database required. Used by sync manager unit tests so
    the classification logic (new/modified/unchanged/deleted) can be
    verified without a running Postgres instance.
    """

    def __init__(self) -> None:
        self._store: dict[tuple[SourceType, str], Document] = {}

    async def get_by_sharepoint_item_id(
        self, source_type: SourceType, sharepoint_item_id: str
    ) -> Document | None:
        return self._store.get((source_type, sharepoint_item_id))

    async def upsert(self, data: DocumentUpsert) -> Document:
        key = (data.source_type, data.sharepoint_item_id)
        now = datetime.now(timezone.utc)
        existing = self._store.get(key)
        doc = existing or Document(id=uuid.uuid4(), source_type=data.source_type, sharepoint_item_id=data.sharepoint_item_id, name=data.name)
        doc.drive_id = data.drive_id
        doc.site_id = data.site_id
        doc.name = data.name
        doc.extension = data.extension
        doc.path = data.path
        doc.sharepoint_url = data.sharepoint_url
        doc.parent_sharepoint_item_id = data.parent_sharepoint_item_id
        doc.modified_at = data.modified_at
        doc.etag = data.etag
        doc.size = data.size
        doc.content_hash = data.content_hash
        doc.processing_status = data.processing_status
        doc.processing_error = data.processing_error
        doc.retry_count = data.retry_count
        doc.is_deleted = False
        doc.last_seen_at = now
        doc.last_synced_at = now
        doc.last_processed_at = now
        self._store[key] = doc
        return doc

    async def touch_metadata_only(self, document: Document, seen_at: datetime) -> None:
        document.last_seen_at = seen_at
        document.last_synced_at = seen_at

    async def mark_deleted(self, source_type: SourceType, sharepoint_item_id: str) -> bool:
        doc = self._store.get((source_type, sharepoint_item_id))
        if doc is None:
            return False
        doc.is_deleted = True
        return True

    async def mark_missing_deleted(self, source_type: SourceType, seen_item_ids: set[str]) -> int:
        count = 0
        for (stype, item_id), doc in self._store.items():
            if stype == source_type and not doc.is_deleted and item_id not in seen_item_ids:
                doc.is_deleted = True
                count += 1
        return count

    async def mark_processing_status(
        self, document_id: uuid.UUID, status: ProcessingStatus, error: str | None = None
    ) -> None:
        for doc in self._store.values():
            if doc.id == document_id:
                doc.processing_status = status
                doc.processing_error = error
                return

    async def rollback(self) -> None:
        pass  # nothing to roll back in an in-memory store

    async def commit(self) -> None:
        pass  # every write is already "durable" in an in-memory store

    async def get_by_id(self, document_id: uuid.UUID) -> Document | None:
        return next((doc for doc in self._store.values() if doc.id == document_id), None)

    async def list_by_processing_status(self, statuses: set[ProcessingStatus]) -> list[Document]:
        return [
            doc for doc in self._store.values()
            if doc.processing_status in statuses and not doc.is_deleted
        ]


class SyncStateRepository(Protocol):
    """
    Spec section 27: one row per source (e.g. "document_library"), holding
    the delta cursor. Spec section 21: the cursor must only ever be
    persisted via record_success -- there is deliberately no generic
    "save cursor" method, so a caller cannot accidentally advance it before
    processing has actually finished successfully.
    """

    async def get_delta_link(self, source: str) -> str | None: ...

    async def record_attempt(self, source: str) -> None: ...

    async def record_success(self, source: str, delta_link: str | None) -> None: ...


class SqlAlchemySyncStateRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_delta_link(self, source: str) -> str | None:
        row = await self._session.get(SyncState, source)
        return row.delta_link if row else None

    async def record_attempt(self, source: str) -> None:
        now = datetime.now(timezone.utc)
        row = await self._session.get(SyncState, source)
        if row is None:
            row = SyncState(source=source, last_attempted_sync=now, status="running")
            self._session.add(row)
        else:
            row.last_attempted_sync = now
            row.status = "running"
        await self._session.flush()

    async def record_success(self, source: str, delta_link: str | None) -> None:
        now = datetime.now(timezone.utc)
        row = await self._session.get(SyncState, source)
        if row is None:
            row = SyncState(
                source=source, delta_link=delta_link,
                last_successful_sync=now, last_attempted_sync=now, status="ok",
            )
            self._session.add(row)
        else:
            row.delta_link = delta_link
            row.last_successful_sync = now
            row.status = "ok"
        await self._session.flush()


class InMemorySyncStateRepository:
    """Test double -- mirrors SqlAlchemySyncStateRepository without a database."""

    def __init__(self) -> None:
        self._store: dict[str, str | None] = {}

    async def get_delta_link(self, source: str) -> str | None:
        return self._store.get(source)

    async def record_attempt(self, source: str) -> None:
        pass  # nothing meaningful to track without real persistence

    async def record_success(self, source: str, delta_link: str | None) -> None:
        self._store[source] = delta_link


class ChunkRepository(Protocol):
    async def replace_chunks(self, document_id: uuid.UUID, chunks: list[Chunk]) -> None:
        """
        Deletes every existing chunk for this document and inserts the new
        set -- atomically, within one call. Spec section 57: never leave a
        document with some old chunks deleted and no new ones in place.
        Safe to call with an empty chunk list (e.g. extraction produced no
        text) -- that just clears whatever chunks existed before.
        """
        ...

    async def load_chunks(self, document_id: uuid.UUID) -> list[Chunk]:
        """A document's chunks in chunk_index order."""
        ...

    async def set_embeddings(
        self, document_id: uuid.UUID, vectors: dict[uuid.UUID, list[float]], model_name: str
    ) -> None:
        """Stores one vector per chunk id, recording the model that produced it."""
        ...

    async def document_ids_with_other_embedding_model(self, model_name: str) -> set[uuid.UUID]:
        """
        Documents holding any chunk embedded by a model other than
        model_name -- i.e. stale after a model change (spec section 35).
        """
        ...


class SqlAlchemyChunkRepository:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def replace_chunks(self, document_id: uuid.UUID, chunks: list[Chunk]) -> None:
        await self._session.execute(delete(ChunkModel).where(ChunkModel.document_id == document_id))
        for chunk in chunks:
            self._session.add(ChunkModel(
                id=chunk.id,
                document_id=document_id,
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                token_count=chunk.token_count,
                metadata_=chunk.metadata,
            ))
        await self._session.flush()

    async def load_chunks(self, document_id: uuid.UUID) -> list[Chunk]:
        stmt = (
            select(ChunkModel)
            .where(ChunkModel.document_id == document_id)
            .order_by(ChunkModel.chunk_index)
        )
        result = await self._session.execute(stmt)
        return [
            Chunk(
                id=row.id,
                chunk_index=row.chunk_index,
                content=row.content,
                token_count=row.token_count,
                metadata=row.metadata_,
            )
            for row in result.scalars().all()
        ]

    async def set_embeddings(
        self, document_id: uuid.UUID, vectors: dict[uuid.UUID, list[float]], model_name: str
    ) -> None:
        for chunk_id, vector in vectors.items():
            await self._session.execute(
                update(ChunkModel)
                .where(ChunkModel.id == chunk_id, ChunkModel.document_id == document_id)
                .values(embedding=vector, embedding_model=model_name)
            )
        await self._session.flush()

    async def document_ids_with_other_embedding_model(self, model_name: str) -> set[uuid.UUID]:
        stmt = select(ChunkModel.document_id).where(
            ChunkModel.embedding_model.is_not(None),
            ChunkModel.embedding_model != model_name,
        ).distinct()
        result = await self._session.execute(stmt)
        return set(result.scalars().all())


class InMemoryChunkRepository:
    """Test double -- mirrors SqlAlchemyChunkRepository without a database."""

    def __init__(self) -> None:
        self._store: dict[uuid.UUID, list[Chunk]] = {}
        # chunk_id -> (vector, model_name); replace_chunks drops a
        # document's entries, mirroring the row delete in the SQL version.
        self.embeddings: dict[uuid.UUID, tuple[list[float], str]] = {}

    async def replace_chunks(self, document_id: uuid.UUID, chunks: list[Chunk]) -> None:
        for old in self._store.get(document_id, []):
            self.embeddings.pop(old.id, None)
        self._store[document_id] = list(chunks)

    def get_chunks(self, document_id: uuid.UUID) -> list[Chunk]:
        """Test-only synchronous accessor, not part of the ChunkRepository protocol."""
        return self._store.get(document_id, [])

    async def load_chunks(self, document_id: uuid.UUID) -> list[Chunk]:
        return sorted(self._store.get(document_id, []), key=lambda c: c.chunk_index)

    async def set_embeddings(
        self, document_id: uuid.UUID, vectors: dict[uuid.UUID, list[float]], model_name: str
    ) -> None:
        known = {c.id for c in self._store.get(document_id, [])}
        for chunk_id, vector in vectors.items():
            if chunk_id in known:
                self.embeddings[chunk_id] = (list(vector), model_name)

    async def document_ids_with_other_embedding_model(self, model_name: str) -> set[uuid.UUID]:
        return {
            document_id
            for document_id, chunks in self._store.items()
            if any(
                c.id in self.embeddings and self.embeddings[c.id][1] != model_name
                for c in chunks
            )
        }
