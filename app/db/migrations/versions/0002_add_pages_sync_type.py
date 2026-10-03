"""add 'pages' value to sync_run_type_enum

Revision ID: 0002_add_pages_sync_type
Revises: 0001_initial_schema
Create Date: 2026-09-27

"""
from alembic import op

revision = "0002_add_pages_sync_type"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Postgres requires ALTER TYPE ... ADD VALUE to run outside a normal
    # transaction block (a value added inside a transaction isn't visible
    # for use until that transaction commits, and older Postgres versions
    # reject it outright). autocommit_block() runs this statement in its
    # own connection outside Alembic's usual per-migration transaction.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE sync_run_type_enum ADD VALUE IF NOT EXISTS 'pages'")


def downgrade() -> None:
    # Postgres has no native "remove one enum value" operation. Doing this
    # properly requires rebuilding the type (create a new type without the
    # value, migrate the column over, drop the old type) -- not worth the
    # risk to implement blindly for a downgrade path that's unlikely to be
    # exercised. If you need to roll back past this revision, restore from
    # a backup taken before it.
    raise NotImplementedError(
        "Cannot cleanly remove an enum value in Postgres. "
        "Restore from a pre-migration backup if you need to downgrade past this revision."
    )
