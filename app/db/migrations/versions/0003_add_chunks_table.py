"""add chunks table and CHUNKED processing_status value

Revision ID: 0003_add_chunks_table
Revises: 0002_add_pages_sync_type
Create Date: 2026-09-29

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_add_chunks_table"
down_revision = "0002_add_pages_sync_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Same reasoning as migration 0002: ALTER TYPE ... ADD VALUE cannot run
    # inside Alembic's normal per-migration transaction on Postgres.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE processing_status_enum ADD VALUE IF NOT EXISTS 'CHUNKED'")

    op.create_table(
        "chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_chunks_document_id", "chunks", ["document_id"])


def downgrade() -> None:
    # Raised before any DDL runs -- Postgres can't cleanly remove an enum
    # value ('CHUNKED' would remain in processing_status_enum regardless of
    # whether the chunks table is dropped), so there's no partial-downgrade
    # story worth attempting here.
    raise NotImplementedError(
        "Cannot cleanly downgrade: Postgres has no 'drop enum value' for "
        "the 'CHUNKED' value added to processing_status_enum. Restore from "
        "a pre-migration backup if you need to roll back past this revision."
    )
