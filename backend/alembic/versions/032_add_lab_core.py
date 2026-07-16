"""Lab core (experiment building P1): escrow holds, researcher treasuries,
lab tasks / runs / steps / artifacts.

WRITE ONLY — do not run during burn-in. Verify on real Postgres before deploy.
Chains onto 031_add_home_decor.

Revision ID: 032_add_lab_core
Revises: 031_add_home_decor
Create Date: 2026-07-16

"""
from alembic import op
import sqlalchemy as sa

revision = "032_add_lab_core"
down_revision = "031_add_home_decor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "coin_holds",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("user_id", sa.String(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="held"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_coin_holds_user_id", "coin_holds", ["user_id"])
    op.create_index("ix_coin_holds_status", "coin_holds", ["status"])

    op.create_table(
        "resident_treasuries",
        sa.Column("resident_slug", sa.String(length=100), primary_key=True),
        sa.Column("balance_sc", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "lab_tasks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("issuer_user_id", sa.String(), nullable=False),
        sa.Column("researcher_slug", sa.String(length=100), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("brief_md", sa.Text(), nullable=False, server_default=""),
        sa.Column("scopes_json", sa.JSON(), nullable=True),
        sa.Column("reward_sc", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("platform_fee_sc", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deliverable_kind", sa.String(length=20), nullable=False, server_default="report"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column("hold_id", sa.String(), nullable=True),
        sa.Column("accepted_run_id", sa.String(), nullable=True),
        sa.Column("reject_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result_summary_md", sa.Text(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("review_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_lab_tasks_issuer_user_id", "lab_tasks", ["issuer_user_id"])
    op.create_index("ix_lab_tasks_researcher_slug", "lab_tasks", ["researcher_slug"])
    op.create_index("ix_lab_tasks_status", "lab_tasks", ["status"])

    op.create_table(
        "lab_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("researcher_slug", sa.String(length=100), nullable=False),
        sa.Column("adapter", sa.String(length=20), nullable=False, server_default="mock"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("scopes_json", sa.JSON(), nullable=True),
        sa.Column("budget_usd_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_usd_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("approvals_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_lab_runs_task_id", "lab_runs", ["task_id"])
    op.create_index("ix_lab_runs_status", "lab_runs", ["status"])

    op.create_table(
        "lab_run_steps",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(length=20), nullable=False),
        sa.Column("tool", sa.String(length=60), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_lab_run_steps_run_id", "lab_run_steps", ["run_id"])

    op.create_table(
        "lab_artifacts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="text"),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("uri", sa.String(length=1000), nullable=True),
        sa.Column("text_md", sa.Text(), nullable=True),
        sa.Column("meta_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_lab_artifacts_run_id", "lab_artifacts", ["run_id"])
    op.create_index("ix_lab_artifacts_task_id", "lab_artifacts", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_lab_artifacts_task_id", table_name="lab_artifacts")
    op.drop_index("ix_lab_artifacts_run_id", table_name="lab_artifacts")
    op.drop_table("lab_artifacts")
    op.drop_index("ix_lab_run_steps_run_id", table_name="lab_run_steps")
    op.drop_table("lab_run_steps")
    op.drop_index("ix_lab_runs_status", table_name="lab_runs")
    op.drop_index("ix_lab_runs_task_id", table_name="lab_runs")
    op.drop_table("lab_runs")
    op.drop_index("ix_lab_tasks_status", table_name="lab_tasks")
    op.drop_index("ix_lab_tasks_researcher_slug", table_name="lab_tasks")
    op.drop_index("ix_lab_tasks_issuer_user_id", table_name="lab_tasks")
    op.drop_table("lab_tasks")
    op.drop_table("resident_treasuries")
    op.drop_index("ix_coin_holds_status", table_name="coin_holds")
    op.drop_index("ix_coin_holds_user_id", table_name="coin_holds")
    op.drop_table("coin_holds")
