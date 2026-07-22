"""add production artifact lifecycle and durable operations

Revision ID: 043_lab_artifact_pipeline
Revises: 042_lab_world_fencing
Create Date: 2026-07-22
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "043_lab_artifact_pipeline"
down_revision = "042_lab_world_fencing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "lab_tool_executions",
        sa.Column("result_receipt_json", sa.JSON(), nullable=True),
    )
    columns = (
        sa.Column("provider_artifact_id", sa.String(length=200), nullable=True),
        sa.Column("runtime_session_id", sa.String(length=36), nullable=True),
        sa.Column("provider_session_id", sa.String(length=200), nullable=True),
        sa.Column("producer_epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("declared_content_type", sa.String(length=200), nullable=True),
        sa.Column("content_type", sa.String(length=200), nullable=True),
        sa.Column("original_filename", sa.String(length=255), nullable=True),
        sa.Column("expected_sha256", sa.String(length=64), nullable=True),
        sa.Column("declared_byte_size", sa.Integer(), nullable=True),
        sa.Column("storage_backend", sa.String(length=30), nullable=True),
        sa.Column(
            "storage_status",
            sa.String(length=24),
            nullable=False,
            server_default="legacy",
        ),
        sa.Column("quarantine_bucket", sa.String(length=255), nullable=True),
        sa.Column("quarantine_key", sa.String(length=1024), nullable=True),
        sa.Column("quarantine_version_id", sa.String(length=255), nullable=True),
        sa.Column("quarantine_etag", sa.String(length=255), nullable=True),
        sa.Column("released_bucket", sa.String(length=255), nullable=True),
        sa.Column("released_key", sa.String(length=1024), nullable=True),
        sa.Column("released_version_id", sa.String(length=255), nullable=True),
        sa.Column("released_etag", sa.String(length=255), nullable=True),
        sa.Column("scan_policy_version", sa.String(length=100), nullable=True),
        sa.Column("scan_job_id", sa.String(length=200), nullable=True),
        sa.Column("scan_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scan_engine_version", sa.String(length=100), nullable=True),
        sa.Column("scan_error_code", sa.String(length=100), nullable=True),
        sa.Column("upload_receipt_digest", sa.String(length=64), nullable=True),
        sa.Column("scan_receipt_digest", sa.String(length=64), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
    )
    for column in columns:
        op.add_column("lab_artifacts", column)

    op.create_unique_constraint(
        "uq_lab_artifacts_run_provider_artifact",
        "lab_artifacts",
        ["run_id", "provider_artifact_id"],
    )
    op.create_check_constraint(
        "ck_lab_artifacts_storage_status",
        "lab_artifacts",
        "storage_status IN ('legacy','pending_upload','quarantined','released',"
        "'delete_pending','deleted')",
    )
    op.create_check_constraint(
        "ck_lab_artifacts_scan_status",
        "lab_artifacts",
        "scan_status IN ('skipped','pending','scanning','clean','flagged','failed')",
    )
    op.create_check_constraint(
        "ck_lab_artifacts_verification_status",
        "lab_artifacts",
        "verification_status IN ('unverified','verified','rejected')",
    )
    op.create_check_constraint(
        "ck_lab_artifacts_byte_size", "lab_artifacts", "byte_size >= 0"
    )
    op.create_check_constraint(
        "ck_lab_artifacts_declared_byte_size",
        "lab_artifacts",
        "declared_byte_size IS NULL OR declared_byte_size >= 0",
    )
    op.create_check_constraint(
        "ck_lab_artifacts_scan_attempts", "lab_artifacts", "scan_attempts >= 0"
    )
    op.create_check_constraint(
        "ck_lab_artifacts_producer_epoch", "lab_artifacts", "producer_epoch >= 0"
    )
    op.create_check_constraint(
        "ck_lab_artifacts_row_version", "lab_artifacts", "row_version >= 1"
    )
    op.create_index(
        "ix_lab_artifacts_storage_status", "lab_artifacts", ["storage_status"]
    )
    op.create_index(
        "ix_lab_artifacts_scan_job_id", "lab_artifacts", ["scan_job_id"]
    )

    op.create_table(
        "lab_artifact_operations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("operation_id", sa.String(length=200), nullable=False),
        sa.Column(
            "artifact_id",
            sa.String(),
            sa.ForeignKey("lab_artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operation_type", sa.String(length=20), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("command_digest", sa.String(length=64), nullable=False),
        sa.Column("command_json", sa.JSON(), nullable=False),
        sa.Column("service_endpoint", sa.String(length=2048), nullable=True),
        sa.Column("job_id", sa.String(length=200), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("receipt_json", sa.JSON(), nullable=True),
        sa.Column("receipt_digest", sa.String(length=64), nullable=True),
        sa.Column("accounted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "operation_id", name="uq_lab_artifact_operations_operation"
        ),
        sa.CheckConstraint(
            "operation_type IN ('upload','scan','delete')",
            name="ck_lab_artifact_operations_type",
        ),
        sa.CheckConstraint(
            "state IN ('pending','processing','succeeded','failed','quarantined')",
            name="ck_lab_artifact_operations_state",
        ),
        sa.CheckConstraint("epoch >= 0", name="ck_lab_artifact_operations_epoch"),
        sa.CheckConstraint(
            "attempt >= 0", name="ck_lab_artifact_operations_attempt"
        ),
    )
    op.create_index(
        "ix_lab_artifact_operations_artifact_id",
        "lab_artifact_operations",
        ["artifact_id"],
    )
    op.create_index(
        "ix_lab_artifact_operations_state", "lab_artifact_operations", ["state"]
    )
    op.create_index(
        "ix_lab_artifact_operations_next_retry_at",
        "lab_artifact_operations",
        ["next_retry_at"],
    )

    op.create_table(
        "lab_artifact_holds",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "artifact_id",
            sa.String(),
            sa.ForeignKey("lab_artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(length=200), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("source_id", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "source_type IN ('task','world_proposal','manual','legal')",
            name="ck_lab_artifact_holds_source_type",
        ),
    )
    op.create_index(
        "ix_lab_artifact_holds_artifact_id",
        "lab_artifact_holds",
        ["artifact_id"],
    )
    op.create_index(
        "ix_lab_artifact_holds_released_at",
        "lab_artifact_holds",
        ["released_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_lab_artifact_holds_released_at", table_name="lab_artifact_holds")
    op.drop_index("ix_lab_artifact_holds_artifact_id", table_name="lab_artifact_holds")
    op.drop_table("lab_artifact_holds")

    op.drop_index(
        "ix_lab_artifact_operations_next_retry_at",
        table_name="lab_artifact_operations",
    )
    op.drop_index(
        "ix_lab_artifact_operations_state", table_name="lab_artifact_operations"
    )
    op.drop_index(
        "ix_lab_artifact_operations_artifact_id",
        table_name="lab_artifact_operations",
    )
    op.drop_table("lab_artifact_operations")

    op.drop_index("ix_lab_artifacts_scan_job_id", table_name="lab_artifacts")
    op.drop_index("ix_lab_artifacts_storage_status", table_name="lab_artifacts")
    for constraint in (
        "ck_lab_artifacts_row_version",
        "ck_lab_artifacts_producer_epoch",
        "ck_lab_artifacts_scan_attempts",
        "ck_lab_artifacts_declared_byte_size",
        "ck_lab_artifacts_byte_size",
        "ck_lab_artifacts_verification_status",
        "ck_lab_artifacts_scan_status",
        "ck_lab_artifacts_storage_status",
        "uq_lab_artifacts_run_provider_artifact",
    ):
        op.drop_constraint(constraint, "lab_artifacts", type_="unique" if constraint.startswith("uq_") else "check")
    for column in (
        "row_version",
        "deleted_at",
        "released_at",
        "scan_receipt_digest",
        "upload_receipt_digest",
        "scan_error_code",
        "scan_engine_version",
        "scanned_at",
        "scan_attempts",
        "scan_job_id",
        "scan_policy_version",
        "released_etag",
        "released_version_id",
        "released_key",
        "released_bucket",
        "quarantine_etag",
        "quarantine_version_id",
        "quarantine_key",
        "quarantine_bucket",
        "storage_status",
        "storage_backend",
        "declared_byte_size",
        "expected_sha256",
        "original_filename",
        "content_type",
        "declared_content_type",
        "required",
        "producer_epoch",
        "provider_session_id",
        "runtime_session_id",
        "provider_artifact_id",
    ):
        op.drop_column("lab_artifacts", column)
    op.drop_column("lab_tool_executions", "result_receipt_json")
