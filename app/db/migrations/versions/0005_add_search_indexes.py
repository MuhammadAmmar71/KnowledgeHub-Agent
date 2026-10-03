"""add HNSW vector index and full-text search column to chunks

Revision ID: 0005_add_search_indexes
Revises: 0004_add_chunk_embeddings
Create Date: 2026-10-03

"""
from alembic import op

revision = "0005_add_search_indexes"
down_revision = "0004_add_chunk_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Cosine ops to match how retrieval orders results (embeddings are
    # normalized, so cosine and inner product rank identically anyway).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    # Generated column: Postgres keeps it in sync with content on every
    # insert/update, so the sync/chunking code never has to know it exists.
    op.execute(
        "ALTER TABLE chunks ADD COLUMN content_tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('english', content)) STORED"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_chunks_content_tsv ON chunks USING gin (content_tsv)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_content_tsv")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS content_tsv")
    op.execute("DROP INDEX IF EXISTS ix_chunks_embedding_hnsw")
