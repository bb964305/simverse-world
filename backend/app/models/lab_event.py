"""Durable event ledger for Lab Runtime Protocol v1 runs, plus the outbox for
at-least-once external delivery (PRD §Protocols, §Data and API Evolution).

``LabRunEvent`` is the append-only per-run event log — (run_id, seq) is the
ordering key a runtime must respect; ``provider_event_id`` (when the runtime
supplies one) is a separate dedup key, nullable so runtimes that don't supply
one can still write freely. ``OutboxEvent`` is a durable, monotonic cursor
(autoincrement id) for a transactional-outbox publisher — rows are only ever
appended or stamped with ``published_at``, never mutated otherwise.
"""
import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Integer, DateTime, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LabRunEvent(Base):
    __tablename__ = "lab_run_events"
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_lab_run_events_run_seq"),
        UniqueConstraint("run_id", "provider_event_id", name="uq_lab_run_events_provider"),
    )

    event_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    run_id: Mapped[str] = mapped_column(String, index=True)
    task_id: Mapped[str] = mapped_column(String)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(60))
    action_id: Mapped[str | None] = mapped_column(String, nullable=True)
    parent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    provider_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    fencing_epoch: Mapped[int] = mapped_column(Integer, default=0)
    policy_version: Mapped[str] = mapped_column(String(20))
    trace_id: Mapped[str | None] = mapped_column(String, nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class OutboxEvent(Base):
    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True)
    tenant_id: Mapped[str] = mapped_column(String)
    run_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    topic: Mapped[str] = mapped_column(String(40))
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Dispatcher state (recovery plan Phase 2, gap #11). ``published_at`` stays
    # the success marker; ``dispatch_status`` adds the dead-letter/quarantine
    # terminal for a row that must never be marked published. A row is eligible
    # when published_at IS NULL AND dispatch_status='pending' AND
    # next_attempt_at<=now AND (locked_until IS NULL OR locked_until<=now).
    dispatch_status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending|published|dead
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
