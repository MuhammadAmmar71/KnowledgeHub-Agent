"""
Persistent document/sync state.

Spec section 24: documents table -- Graph item ID (not filename) is
canonical identity, unique on (source_type, sharepoint_item_id).
Spec section 26: sync_runs -- every synchronization creates one.
Spec section 27: sync_state -- one row per source (document_library /
site_pages), holding the delta cursor. Never store a cursor without
knowing which source it belongs to.
Spec section 25: chunks -- one row per chunk, FK'd to its document,
carrying its pgvector embedding (Phase 7) once the embedding stage has run.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


# Output dimension of the configured embedding model (BAAI/bge-small-en-v1.5).
# Fixed into the column type, so moving to a model with a different
# dimension requires a new migration as well as a settings change -- the
# embedding stage refuses to run on a mismatch rather than failing per row.
EMBEDDING_DIMENSIONS = 384


class Base(DeclarativeBase):
    pass


class SourceType(str, enum.Enum):
    file = "file"
    page = "page"


class ProcessingStatus(str, enum.Enum):
    discovered = "DISCOVERED"
    pending = "PENDING"
    downloading = "DOWNLOADING"
    extracting = "EXTRACTING"
    chunking = "CHUNKING"
    chunked = "CHUNKED"
    embedding = "EMBEDDING"
    indexed = "INDEXED"
    download_failed = "DOWNLOAD_FAILED"
    extraction_failed = "EXTRACTION_FAILED"
    chunking_failed = "CHUNKING_FAILED"
    embedding_failed = "EMBEDDING_FAILED"


class SyncRunType(str, enum.Enum):
    initial = "initial"
    delta = "delta"
    reconciliation = "reconciliation"
    manual = "manual"
    pages = "pages"


class SyncRunStatus(str, enum.Enum):
    running = "running"
    success = "success"
    partial = "partial"
    failed = "failed"


class Document(Base):
    """Spec section 24. One row per file or page, keyed by Graph identity."""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("source_type", "sharepoint_item_id", name="uq_documents_source_item"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    source_type: Mapped[SourceType] = mapped_column(
        Enum(SourceType, name="source_type_enum", values_callable=lambda e: [m.value for m in e])
    )
    sharepoint_item_id: Mapped[str] = mapped_column(String(512), index=True)
    drive_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    site_id: Mapped[str | None] = mapped_column(String(512), nullable=True)

    name: Mapped[str] = mapped_column(String(1024))
    extension: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)

    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    sharepoint_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_sharepoint_item_id: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    etag: Mapped[str | None] = mapped_column(String(255), nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)  # SHA-256 hex

    processing_status: Mapped[ProcessingStatus] = mapped_column(
        Enum(ProcessingStatus, name="processing_status_enum", values_callable=lambda e: [m.value for m in e]),
        default=ProcessingStatus.discovered,
    )
    processing_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)

    is_deleted: Mapped[bool] = mapped_column(default=False)

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_db_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_db_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SyncRun(Base):
    """Spec section 26. Every synchronization (initial/delta/reconciliation/manual) creates one row."""

    __tablename__ = "sync_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    sync_type: Mapped[SyncRunType] = mapped_column(
        Enum(SyncRunType, name="sync_run_type_enum", values_callable=lambda e: [m.value for m in e])
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[SyncRunStatus] = mapped_column(
        Enum(SyncRunStatus, name="sync_run_status_enum", values_callable=lambda e: [m.value for m in e]),
        default=SyncRunStatus.running,
    )

    items_detected: Mapped[int] = mapped_column(Integer, default=0)
    items_added: Mapped[int] = mapped_column(Integer, default=0)
    items_modified: Mapped[int] = mapped_column(Integer, default=0)
    items_deleted: Mapped[int] = mapped_column(Integer, default=0)
    items_skipped: Mapped[int] = mapped_column(Integer, default=0)
    items_failed: Mapped[int] = mapped_column(Integer, default=0)

    error_count: Mapped[int] = mapped_column(Integer, default=0)
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)


class SyncState(Base):
    """Spec section 27. One row per source; never store a cursor without knowing whose it is."""

    __tablename__ = "sync_state"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)  # "document_library" | "site_pages"

    delta_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_successful_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempted_sync: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    status: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Chunk(Base):
    """
    Spec section 25. Deliberately NOT default=uuid.uuid4 for id -- chunk
    IDs are deterministic (app/chunking/chunker.py), derived from
    (document_id, content_hash, chunk_index), so the same content always
    reproduces the same chunk IDs and a genuine content change always
    produces new ones. That determinism is what makes the "delete old
    chunks, insert new ones" replacement in spec section 57 safe.
    """

    __tablename__ = "chunks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )

    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer)

    # "metadata" is reserved on SQLAlchemy's declarative Base (Base.metadata
    # is the schema registry), so the Python attribute is metadata_ while
    # the actual database column is still named "metadata" per spec.
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)

    # NULL until the embedding stage (app/embeddings/indexer.py) has run for
    # this chunk. embedding_model records which model produced the vector
    # (spec section 35) so a model change can be detected and re-embedded.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Keyword side of hybrid search (spec section 36: "optional keyword/
    # full-text signals"). Generated by Postgres from content, so it can
    # never drift out of sync with it -- the app never writes this column.
    content_tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', content)", persisted=True)
    )

    __table_args__ = (
        Index(
            "ix_chunks_embedding_hnsw", "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_content_tsv", "content_tsv", postgresql_using="gin"),
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
