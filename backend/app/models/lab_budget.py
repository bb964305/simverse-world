"""Six-dimension hard budget ledger per Lab run (PRD §Protocols, Broker
enforcement). Eight tracked dimensions — model_tokens, tool_calls,
wall_clock_ms, egress_requests, egress_bytes, artifact_count, artifact_bytes,
active_workers — each with limit_/used_/reserved_ integer columns.
``reserved_`` lets the Broker pre-commit spend before a tool call completes so
two concurrent calls can't both slip under the limit (budget engine itself is
a later task; this is storage only).
"""
from datetime import datetime, UTC

from sqlalchemy import String, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LabRunBudget(Base):
    __tablename__ = "lab_run_budgets"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String)

    limit_model_tokens: Mapped[int] = mapped_column(Integer, default=0)
    used_model_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reserved_model_tokens: Mapped[int] = mapped_column(Integer, default=0)

    limit_tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    used_tool_calls: Mapped[int] = mapped_column(Integer, default=0)
    reserved_tool_calls: Mapped[int] = mapped_column(Integer, default=0)

    limit_wall_clock_ms: Mapped[int] = mapped_column(Integer, default=0)
    used_wall_clock_ms: Mapped[int] = mapped_column(Integer, default=0)
    reserved_wall_clock_ms: Mapped[int] = mapped_column(Integer, default=0)

    limit_egress_requests: Mapped[int] = mapped_column(Integer, default=0)
    used_egress_requests: Mapped[int] = mapped_column(Integer, default=0)
    reserved_egress_requests: Mapped[int] = mapped_column(Integer, default=0)

    limit_egress_bytes: Mapped[int] = mapped_column(Integer, default=0)
    used_egress_bytes: Mapped[int] = mapped_column(Integer, default=0)
    reserved_egress_bytes: Mapped[int] = mapped_column(Integer, default=0)

    limit_artifact_count: Mapped[int] = mapped_column(Integer, default=0)
    used_artifact_count: Mapped[int] = mapped_column(Integer, default=0)
    reserved_artifact_count: Mapped[int] = mapped_column(Integer, default=0)

    limit_artifact_bytes: Mapped[int] = mapped_column(Integer, default=0)
    used_artifact_bytes: Mapped[int] = mapped_column(Integer, default=0)
    reserved_artifact_bytes: Mapped[int] = mapped_column(Integer, default=0)

    limit_active_workers: Mapped[int] = mapped_column(Integer, default=0)
    used_active_workers: Mapped[int] = mapped_column(Integer, default=0)
    reserved_active_workers: Mapped[int] = mapped_column(Integer, default=0)

    exhausted_dimension: Mapped[str | None] = mapped_column(String(20), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
