"""Realism P0 columns.

Adds:
- ``memories.archived_at`` (P0-2 soft-archive eviction marker)
- ``residents.pinned_heat`` (P0-5a heat display split; manual/pinned floor)
- ``world_change_proposals.approved_at`` (P0-5b stuck-approved reclaim window)

WRITE ONLY — do not run during burn-in. Verify on real Postgres before deploy
(vm212). SQLite supports native ADD COLUMN, no batch mode needed; drop_column
downgrades are Postgres-only (SQLite can't drop without a table rebuild), per
the 020/031/035 precedent.
Chains onto 037_add_lab_worker_attempts.

Revision ID: 038_add_realism_fields
Revises: 037_add_lab_worker_attempts
Create Date: 2026-07-22

"""
from alembic import op
import sqlalchemy as sa

revision = "038_add_realism_fields"
down_revision = "037_add_lab_worker_attempts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("memories", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("residents", sa.Column("pinned_heat", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("world_change_proposals", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("world_change_proposals", "approved_at")
    op.drop_column("residents", "pinned_heat")
    op.drop_column("memories", "archived_at")
