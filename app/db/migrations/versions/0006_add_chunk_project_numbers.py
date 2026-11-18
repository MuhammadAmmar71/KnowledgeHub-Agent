"""add project_numbers to chunks, backfilled from existing content

Revision ID: 0006_add_chunk_project_numbers
Revises: 0005_add_search_indexes
Create Date: 2026-10-07

"""
from alembic import op

revision = "0006_add_chunk_project_numbers"
down_revision = "0005_add_search_indexes"
branch_labels = None
depends_on = None

# Same rule as app/project_numbers.py (copied, not imported: a migration
# must keep describing what it did even if the app's rule changes later).
_PATTERN = r"(?<![0-9])[0-9]{8}(?![0-9])"


def upgrade() -> None:
    op.execute("ALTER TABLE chunks ADD COLUMN project_numbers varchar(8)[] NOT NULL DEFAULT '{}'")

    # Backfill so existing content is searchable by project immediately,
    # without a reprocess + full sync. Mirrors the chunker: numbers from the
    # chunk text, its heading, and its document's name and path.
    op.execute(f"""
        UPDATE chunks c
        SET project_numbers = sub.numbers
        FROM (
            SELECT c2.id, ARRAY(
                SELECT DISTINCT m[1]
                FROM regexp_matches(
                    concat_ws(' ', c2.content, c2.metadata->>'heading', d.name, d.path),
                    '({_PATTERN})', 'g'
                ) AS m
                ORDER BY 1
            )::varchar(8)[] AS numbers
            FROM chunks c2 JOIN documents d ON d.id = c2.document_id
        ) sub
        WHERE c.id = sub.id AND cardinality(sub.numbers) > 0
    """)

    op.execute("CREATE INDEX IF NOT EXISTS ix_chunks_project_numbers ON chunks USING gin (project_numbers)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_project_numbers")
    op.execute("ALTER TABLE chunks DROP COLUMN IF EXISTS project_numbers")
