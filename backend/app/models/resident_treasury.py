from datetime import datetime, UTC

from sqlalchemy import String, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ResidentTreasury(Base):
    """Researcher treasury — the fuel pool a researcher accrues from completed
    LabTasks and later spends on WorldChangeProposals (spec §4.7).

    Deliberately its own table (not ``residents.meta_json``): every debit is an
    atomic ``UPDATE ... WHERE balance_sc >= amount`` (no TOCTOU, unlike the
    meta_json read-modify-write) and the row is the auditable source of truth.

    NOTE (v0.2 code-reconciliation): the spec text also wanted every treasury
    flow mirrored into the ``transactions`` ledger under a synthetic
    ``treasury:<slug>`` account. ``transactions.user_id`` is a hard FK to
    ``users.id``, so a synthetic account id would violate it — treasury flows
    are therefore recorded here (balance_sc + updated_at) rather than in
    transactions. See archive/2026-07-25/docs/LAB_HANDOFF.md deviations.
    """

    __tablename__ = "resident_treasuries"

    resident_slug: Mapped[str] = mapped_column(String(100), primary_key=True)
    balance_sc: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )
