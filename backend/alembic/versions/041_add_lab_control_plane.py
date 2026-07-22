"""add durable Lab control and delivery claim state

Revision ID: 041_lab_control_plane
Revises: 040_runtime_result_delivery
Create Date: 2026-07-22
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "041_lab_control_plane"
down_revision = "040_runtime_result_delivery"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lab_tool_executions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(), sa.ForeignKey("lab_runs.id"), nullable=False),
        sa.Column("session_id", sa.String(length=36), nullable=True),
        sa.Column("action_id", sa.String(length=100), nullable=False),
        sa.Column("job_locator_json", sa.JSON(), nullable=False),
        sa.Column("executor_epoch", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("submit_receipt_json", sa.JSON(), nullable=True),
        sa.Column("control_receipt_json", sa.JSON(), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "action_id", "executor_epoch", name="uq_lab_tool_executions_action_epoch"
        ),
        sa.CheckConstraint(
            "status IN ('active','fenced','confirmed_stopped','quarantined')",
            name="ck_lab_tool_executions_status",
        ),
        sa.CheckConstraint(
            "executor_epoch >= 0", name="ck_lab_tool_executions_epoch"
        ),
    )
    op.create_index(
        "ix_lab_tool_executions_run_id", "lab_tool_executions", ["run_id"]
    )
    op.create_index(
        "ix_lab_tool_executions_status", "lab_tool_executions", ["status"]
    )

    op.create_table(
        "lab_global_controls",
        sa.Column("id", sa.String(length=20), primary_key=True),
        sa.Column("admission_open", sa.Boolean(), nullable=False),
        sa.Column("fencing_epoch", sa.Integer(), nullable=False),
        sa.Column("active_kill_id", sa.String(length=36), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 'global'", name="ck_lab_global_controls_singleton"),
        sa.CheckConstraint(
            "fencing_epoch >= 0", name="ck_lab_global_controls_epoch"
        ),
    )

    op.create_table(
        "lab_global_kills",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("requested_by", sa.String(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("fencing_epoch", sa.Integer(), nullable=False),
        sa.Column("watermark_run_count", sa.Integer(), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_owner", sa.String(length=100), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "idempotency_key", name="uq_lab_global_kills_idempotency"
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','quarantined')",
            name="ck_lab_global_kills_status",
        ),
        sa.CheckConstraint(
            "fencing_epoch > 0", name="ck_lab_global_kills_epoch"
        ),
        sa.CheckConstraint(
            "watermark_run_count >= 0", name="ck_lab_global_kills_watermark"
        ),
    )
    op.create_index(
        "ix_lab_global_kills_status", "lab_global_kills", ["status"]
    )

    op.create_table(
        "lab_control_targets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "request_id",
            sa.String(length=36),
            sa.ForeignKey("lab_run_control_requests.id"),
            nullable=True,
        ),
        sa.Column(
            "kill_id",
            sa.String(length=36),
            sa.ForeignKey("lab_global_kills.id"),
            nullable=True,
        ),
        sa.Column("run_id", sa.String(), sa.ForeignKey("lab_runs.id"), nullable=False),
        sa.Column("target_kind", sa.String(length=20), nullable=False),
        sa.Column("target_id", sa.String(length=100), nullable=False),
        sa.Column("locator_json", sa.JSON(), nullable=False),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("claim_owner", sa.String(length=100), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("receipt_json", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quarantined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "request_id",
            "target_kind",
            "target_id",
            name="uq_lab_control_targets_request_target",
        ),
        sa.UniqueConstraint(
            "kill_id",
            "target_kind",
            "target_id",
            name="uq_lab_control_targets_kill_target",
        ),
        sa.CheckConstraint(
            "(request_id IS NOT NULL AND kill_id IS NULL) OR "
            "(request_id IS NULL AND kill_id IS NOT NULL)",
            name="ck_lab_control_targets_parent",
        ),
        sa.CheckConstraint(
            "target_kind IN ('runtime','executor')",
            name="ck_lab_control_targets_kind",
        ),
        sa.CheckConstraint(
            "action IN ('cancel','terminate','kill')",
            name="ck_lab_control_targets_action",
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','confirmed_stopped','quarantined')",
            name="ck_lab_control_targets_status",
        ),
        sa.CheckConstraint("epoch >= 0", name="ck_lab_control_targets_epoch"),
        sa.CheckConstraint("attempts >= 0", name="ck_lab_control_targets_attempts"),
    )
    for column in ("request_id", "kill_id", "run_id", "status"):
        op.create_index(
            f"ix_lab_control_targets_{column}", "lab_control_targets", [column]
        )

    op.create_table(
        "lab_queue_claims",
        sa.Column("run_id", sa.String(), sa.ForeignKey("lab_runs.id"), primary_key=True),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("claim_token", sa.String(length=36), nullable=False),
        sa.Column("owner_id", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("claim_token", name="uq_lab_queue_claims_token"),
        sa.CheckConstraint(
            "protocol_version IN (1, 2)", name="ck_lab_queue_claims_protocol"
        ),
        sa.CheckConstraint(
            "status IN ('processing','completed','released','expired')",
            name="ck_lab_queue_claims_status",
        ),
        sa.CheckConstraint("attempts > 0", name="ck_lab_queue_claims_attempts"),
    )
    for column in ("protocol_version", "owner_id", "status"):
        op.create_index(f"ix_lab_queue_claims_{column}", "lab_queue_claims", [column])

def downgrade() -> None:
    for column in reversed(("protocol_version", "owner_id", "status")):
        op.drop_index(f"ix_lab_queue_claims_{column}", table_name="lab_queue_claims")
    op.drop_table("lab_queue_claims")

    for column in reversed(("request_id", "kill_id", "run_id", "status")):
        op.drop_index(f"ix_lab_control_targets_{column}", table_name="lab_control_targets")
    op.drop_table("lab_control_targets")

    op.drop_index("ix_lab_global_kills_status", table_name="lab_global_kills")
    op.drop_table("lab_global_kills")
    op.drop_table("lab_global_controls")

    op.drop_index("ix_lab_tool_executions_status", table_name="lab_tool_executions")
    op.drop_index("ix_lab_tool_executions_run_id", table_name="lab_tool_executions")
    op.drop_table("lab_tool_executions")
