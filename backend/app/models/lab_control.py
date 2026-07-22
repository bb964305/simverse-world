"""Durable protocol-v2 delivery and control state owned by the Lab Runner."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LabRunControlRequest(Base):
    __tablename__ = "lab_run_control_requests"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_lab_run_control_requests_idempotency"
        ),
        UniqueConstraint("active_key", name="uq_lab_run_control_requests_active"),
        CheckConstraint(
            "action IN ('cancel','terminate','kill')",
            name="ck_lab_run_control_requests_action",
        ),
        CheckConstraint(
            "status IN ('pending','processing','completed','failed','cancelled',"
            "'quarantined')",
            name="ck_lab_run_control_requests_status",
        ),
        CheckConstraint(
            "fencing_epoch >= 0", name="ck_lab_run_control_requests_epoch"
        ),
        CheckConstraint("attempts >= 0", name="ck_lab_run_control_requests_attempts"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("lab_runs.id"), nullable=False, index=True
    )
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    active_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    requested_by: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", index=True
    )
    fencing_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    claim_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fenced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    executor_stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class LabToolExecution(Base):
    """Durable Executor job locator and its current control disposition."""

    __tablename__ = "lab_tool_executions"
    __table_args__ = (
        UniqueConstraint(
            "action_id", "executor_epoch", name="uq_lab_tool_executions_action_epoch"
        ),
        CheckConstraint(
            "status IN ('active','fenced','confirmed_stopped','quarantined')",
            name="ck_lab_tool_executions_status",
        ),
        CheckConstraint(
            "executor_epoch >= 0", name="ck_lab_tool_executions_epoch"
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("lab_runs.id"), nullable=False, index=True
    )
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action_id: Mapped[str] = mapped_column(String(100), nullable=False)
    job_locator_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    executor_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="active", index=True
    )
    submit_receipt_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    result_receipt_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    control_receipt_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    quarantined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class LabGlobalControl(Base):
    """Singleton admission switch and global fencing epoch."""

    __tablename__ = "lab_global_controls"
    __table_args__ = (
        CheckConstraint("id = 'global'", name="ck_lab_global_controls_singleton"),
        CheckConstraint(
            "fencing_epoch >= 0", name="ck_lab_global_controls_epoch"
        ),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default="global")
    admission_open: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    fencing_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_kill_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class LabGlobalKill(Base):
    """One immutable global-kill inventory and its aggregate outcome."""

    __tablename__ = "lab_global_kills"
    __table_args__ = (
        UniqueConstraint(
            "idempotency_key", name="uq_lab_global_kills_idempotency"
        ),
        CheckConstraint(
            "status IN ('pending','processing','completed','quarantined')",
            name="ck_lab_global_kills_status",
        ),
        CheckConstraint(
            "fencing_epoch > 0", name="ck_lab_global_kills_epoch"
        ),
        CheckConstraint(
            "watermark_run_count >= 0", name="ck_lab_global_kills_watermark"
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    requested_by: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", index=True
    )
    fencing_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    watermark_run_count: Mapped[int] = mapped_column(Integer, nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    claim_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class LabControlTarget(Base):
    """A Runtime or Executor control effect with a durable receipt."""

    __tablename__ = "lab_control_targets"
    __table_args__ = (
        UniqueConstraint(
            "request_id",
            "target_kind",
            "target_id",
            name="uq_lab_control_targets_request_target",
        ),
        UniqueConstraint(
            "kill_id",
            "target_kind",
            "target_id",
            name="uq_lab_control_targets_kill_target",
        ),
        CheckConstraint(
            "(request_id IS NOT NULL AND kill_id IS NULL) OR "
            "(request_id IS NULL AND kill_id IS NOT NULL)",
            name="ck_lab_control_targets_parent",
        ),
        CheckConstraint(
            "target_kind IN ('runtime','executor')",
            name="ck_lab_control_targets_kind",
        ),
        CheckConstraint(
            "action IN ('cancel','terminate','kill')",
            name="ck_lab_control_targets_action",
        ),
        CheckConstraint(
            "status IN ('pending','processing','confirmed_stopped','quarantined')",
            name="ck_lab_control_targets_status",
        ),
        CheckConstraint("epoch >= 0", name="ck_lab_control_targets_epoch"),
        CheckConstraint("attempts >= 0", name="ck_lab_control_targets_attempts"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    request_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("lab_run_control_requests.id"), nullable=True, index=True
    )
    kill_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("lab_global_kills.id"), nullable=True, index=True
    )
    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("lab_runs.id"), nullable=False, index=True
    )
    target_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    target_id: Mapped[str] = mapped_column(String(100), nullable=False)
    locator_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", index=True
    )
    claim_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    deadline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    receipt_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    quarantined_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class LabQueueClaim(Base):
    """Database ownership token for an item moved to a Redis processing list."""

    __tablename__ = "lab_queue_claims"
    __table_args__ = (
        UniqueConstraint("claim_token", name="uq_lab_queue_claims_token"),
        CheckConstraint(
            "protocol_version IN (1, 2)", name="ck_lab_queue_claims_protocol"
        ),
        CheckConstraint(
            "status IN ('processing','completed','released','expired')",
            name="ck_lab_queue_claims_status",
        ),
        CheckConstraint("attempts > 0", name="ck_lab_queue_claims_attempts"),
    )

    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("lab_runs.id"), primary_key=True
    )
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    claim_token: Mapped[str] = mapped_column(String(36), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="processing", index=True
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    claimed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    claim_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
