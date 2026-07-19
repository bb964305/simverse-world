"""Durable specialist-worker attempt table (recovery plan Phase 6). One row per
delegated child worker execution — the locator a supervisor restart reconstructs
live worker slots from (grant JTI + child runtime id + cursor) and the audit of
what each worker produced (status + content-free result digest + cleanup
evidence). All columns structural, none content-bearing.

New table only — no ALTER — so it is SQLite-safe both ways.

WRITE ONLY — do not run during burn-in. Verify on real Postgres before deploy.
Chains onto 036_add_outbox_dispatch.

Revision ID: 037_add_lab_worker_attempts
Revises: 036_add_outbox_dispatch
Create Date: 2026-07-20

"""
from alembic import op
import sqlalchemy as sa

revision = "037_add_lab_worker_attempts"
down_revision = "036_add_outbox_dispatch"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lab_worker_attempts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("parent_action_id", sa.String(), nullable=True),
        sa.Column("role", sa.String(length=30), nullable=False),
        sa.Column("agent_id", sa.String(length=60), nullable=False),
        sa.Column("grant_jti", sa.String(), nullable=True),
        sa.Column("child_runtime_id", sa.String(), nullable=True),
        sa.Column("sub_goal_hash", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="running"),
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("cursor", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_digest", sa.String(length=64), nullable=True),
        sa.Column("fencing_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cleanup_evidence", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_lab_worker_attempts_run_id", "lab_worker_attempts", ["run_id"])
    op.create_index("ix_lab_worker_attempts_grant_jti", "lab_worker_attempts", ["grant_jti"])
    op.create_index("ix_lab_worker_attempts_status", "lab_worker_attempts", ["status"])


def downgrade() -> None:
    op.drop_index("ix_lab_worker_attempts_status", table_name="lab_worker_attempts")
    op.drop_index("ix_lab_worker_attempts_grant_jti", table_name="lab_worker_attempts")
    op.drop_index("ix_lab_worker_attempts_run_id", table_name="lab_worker_attempts")
    op.drop_table("lab_worker_attempts")
