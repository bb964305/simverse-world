"""Durable specialist-worker attempt record (recovery plan Phase 6).

One row per delegated child worker execution. It is the durable locator a
supervisor restart reconstructs live worker slots from (grant JTI + child runtime
id + cursor/checkpoint), and the audit trail of what each worker produced
(terminal status + content-free result digest + cleanup evidence). Everything
here is STRUCTURAL — role, ids, hashes, status, counters — never content.
"""
import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Integer, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LabWorkerAttempt(Base):
    __tablename__ = "lab_worker_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(String, index=True)
    parent_action_id: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String(30))
    agent_id: Mapped[str] = mapped_column(String(60))
    grant_jti: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    child_runtime_id: Mapped[str | None] = mapped_column(String, nullable=True)  # Mock child locator
    sub_goal_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)  # content-free
    status: Mapped[str] = mapped_column(String(12), default="running", index=True)  # running|succeeded|failed|cancelled
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    cursor: Mapped[int] = mapped_column(Integer, default=0)          # provider cursor / checkpoint
    result_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fencing_epoch: Mapped[int] = mapped_column(Integer, default=0)
    cleanup_evidence: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
