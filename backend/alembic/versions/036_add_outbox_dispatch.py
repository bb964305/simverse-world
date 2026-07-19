"""Durable outbox dispatcher state (recovery plan Phase 2, gap #11) — DB-slice on
the existing OutboxEvent. Adds dispatch_status/attempts/next_attempt_at/
locked_until/last_error so a claimant/retry/topic-router can drain the outbox
that append_event / world_revision_service / lab_artifact_service already write.
All columns are defaulted or nullable so pre-existing rows keep their meaning
(published_at stays the success marker; dispatch_status defaults to 'pending').

Plain ``op.add_column`` — SQLite supports native ADD COLUMN, no batch mode
needed. Downgrade uses ``op.drop_column`` following the 020/021/031/035
precedent; SQLite cannot drop a column without a full table rebuild, so
downgrade is Postgres-only — verify there before deploy.

WRITE ONLY — do not run during burn-in. Verify on real Postgres before deploy.
Chains onto 035_add_artifact_integrity.

Revision ID: 036_add_outbox_dispatch
Revises: 035_add_artifact_integrity
Create Date: 2026-07-19

"""
from alembic import op
import sqlalchemy as sa

revision = "036_add_outbox_dispatch"
down_revision = "035_add_artifact_integrity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outbox_events", sa.Column("dispatch_status", sa.String(length=12), nullable=False, server_default="pending"))
    op.create_index("ix_outbox_events_dispatch_status", "outbox_events", ["dispatch_status"])
    op.add_column("outbox_events", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("outbox_events", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox_events", sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("outbox_events", sa.Column("last_error", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("outbox_events", "last_error")
    op.drop_column("outbox_events", "locked_until")
    op.drop_column("outbox_events", "next_attempt_at")
    op.drop_column("outbox_events", "attempts")
    op.drop_index("ix_outbox_events_dispatch_status", table_name="outbox_events")
    op.drop_column("outbox_events", "dispatch_status")
