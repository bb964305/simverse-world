"""Tool-call intents brokered on behalf of a Lab run (Grant/Policy/Broker
boundary, PRD §Protocols). ``LabToolAction`` is the append-only record of
every requested tool call and its lifecycle; ``LabApproval`` is the
human-review gate for actions the Policy Engine marks 'ask' (never created for
a hard 'deny').
"""
import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Integer, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LabToolAction(Base):
    """status: requested | denied | waiting_approval | approved | executing |
    succeeded | failed | cancelled | reconciliation_required
    """

    __tablename__ = "lab_tool_actions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    run_id: Mapped[str] = mapped_column(String, index=True)
    task_id: Mapped[str] = mapped_column(String)
    tool_name: Mapped[str] = mapped_column(String(80))
    tool_version: Mapped[str] = mapped_column(String(20), default="1")
    args_hash: Mapped[str] = mapped_column(String(64))
    args_redacted_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    risk_class: Mapped[str] = mapped_column(String(4))
    status: Mapped[str] = mapped_column(String(30), default="requested", index=True)
    grant_jti: Mapped[str | None] = mapped_column(String, nullable=True)
    fencing_epoch: Mapped[int] = mapped_column(Integer, default=0)
    policy_version: Mapped[str] = mapped_column(String(20))
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    approval_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    artifact_id: Mapped[str | None] = mapped_column(String, nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )


class LabApproval(Base):
    """decision: pending | approved | denied | expired"""

    __tablename__ = "lab_approvals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    run_id: Mapped[str] = mapped_column(String, index=True)
    task_id: Mapped[str] = mapped_column(String)
    action_id: Mapped[str] = mapped_column(String, unique=True)
    preview_json: Mapped[dict] = mapped_column(JSON, default=dict)
    args_digest: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    decided_by: Mapped[str | None] = mapped_column(String, nullable=True)
    decision_scope: Mapped[str] = mapped_column(String(30), default="task_owner")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fencing_epoch: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
