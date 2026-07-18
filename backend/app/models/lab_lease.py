"""Run ownership lease for fencing (Grant/Policy/Broker boundary, PRD
§Protocols). Exactly one live ``owner_id`` may hold a run's lease at a time;
``fencing_epoch`` increments on takeover so a stale writer's actions/events
(carrying the old epoch) are rejected downstream by the Broker/Ledger (later
tasks).
"""
from datetime import datetime, UTC

from sqlalchemy import String, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LabRunLease(Base):
    __tablename__ = "lab_run_leases"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(80))
    fencing_epoch: Mapped[int] = mapped_column(Integer, default=0)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )
