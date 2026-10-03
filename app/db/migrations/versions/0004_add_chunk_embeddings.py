"""add pgvector embedding columns to chunks

Revision ID: 0004_add_chunk_embeddings
Revises: 0003_add_chunks_table
Create Date: 2026-10-03

"""
from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "0004_add_chunk_embeddings"
down_revision = "0003_add_chunks_table"
branch_labels = None
depends_on = None

# Must match EMBEDDING_DIMENSIONS in app/db/models.py at the time this
# migration was written -- hardcoded on purpose, since a migration must
# describe a fixed schema even if the model constant changes later.
_EMBEDDING_DIMENSIONS = 384


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column("chunks", sa.Column("embedding", Vector(_EMBEDDING_DIMENSIONS), nullable=True))
    op.add_column("chunks", sa.Column("embedding_model", sa.String(255), nullable=True))


def downgrade() -> None:
    # The vector extension is left installed -- other objects may depend on
    # it, and leaving an unused extension in place is harmless.
    op.drop_column("chunks", "embedding_model")
    op.drop_column("chunks", "embedding")
