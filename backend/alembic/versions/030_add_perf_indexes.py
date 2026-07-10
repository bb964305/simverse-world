"""P1-3 perf indexes: residents.status + conversations(resident_id, rating).

Chains onto 029_add_debates. Both indexes back hot read paths:
- residents.status: the agent loop filters ``status NOT IN (...)`` every tick.
- conversations(resident_id, rating): per-resident star-rating aggregation.

Revision ID: 030_add_perf_indexes
Revises: 029_add_debates
Create Date: 2026-07-09

"""
from alembic import op

revision = "030_add_perf_indexes"
down_revision = "029_add_debates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_residents_status", "residents", ["status"])
    op.create_index("ix_conversations_resident_rating", "conversations", ["resident_id", "rating"])


def downgrade() -> None:
    op.drop_index("ix_conversations_resident_rating", table_name="conversations")
    op.drop_index("ix_residents_status", table_name="residents")
