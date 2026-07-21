"""Durable protocol-v2 control intents submitted for Runner ownership."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
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
