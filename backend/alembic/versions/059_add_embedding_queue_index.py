"""Index the FIFO lane used by the memory embedding compensation queue.

Only active event rows with a missing vector enter this index.  The worker's
world-event trivia exclusion is intentionally a residual JSON predicate so the
index remains portable between PostgreSQL and SQLite.

Revision ID: 059_add_embedding_queue_index
Revises: 058_add_town_ledger
Create Date: 2026-08-11
"""
from alembic import op
import sqlalchemy as sa


revision = "059_add_embedding_queue_index"
down_revision = "058_add_town_ledger"
branch_labels = None
depends_on = None


_QUEUE_WHERE = "type = 'event' AND embedding IS NULL AND archived_at IS NULL"
_INDEX_NAME = "ix_memories_embedding_backfill_queue"


def upgrade() -> None:
    kwargs = {
        "unique": False,
        "postgresql_where": sa.text(_QUEUE_WHERE),
        "sqlite_where": sa.text(_QUEUE_WHERE),
    }
    if op.get_bind().dialect.name == "postgresql":
        # The production table is hot; avoid blocking memory writers while the
        # partial index is built. Alembic's autocommit block is required by
        # PostgreSQL for CREATE INDEX CONCURRENTLY.
        with op.get_context().autocommit_block():
            op.create_index(
                _INDEX_NAME,
                "memories",
                ["created_at", "id"],
                postgresql_concurrently=True,
                **kwargs,
            )
        return
    op.create_index(_INDEX_NAME, "memories", ["created_at", "id"], **kwargs)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.drop_index(
                _INDEX_NAME,
                table_name="memories",
                postgresql_concurrently=True,
            )
        return
    op.drop_index(_INDEX_NAME, table_name="memories")
