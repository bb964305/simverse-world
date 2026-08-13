"""Append-only audit ledger for the town's public account.

``town_treasuries.balance_sc`` remains the balance source of truth.  This
table records every forward balance movement so operators can reconcile that
scalar without pretending the town is a ``users`` row (the legacy
``transactions`` table has a hard user foreign key).
"""
from datetime import UTC, datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TownTreasuryEntry(Base):
    __tablename__ = "town_treasury_entries"
    __table_args__ = (
        Index("ix_town_treasury_entries_created_at", "created_at"),
        Index("ix_town_treasury_entries_reason", "reason"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    town_key: Mapped[str] = mapped_column(
        String(100), ForeignKey("town_treasuries.key", ondelete="CASCADE"),
        nullable=False,
    )
    # Signed from the town's perspective: income > 0, spending < 0.
    amount_sc: Mapped[int] = mapped_column(Integer, nullable=False)
    # Balance returned by the same SQL statement that moved the scalar account.
    # This makes concurrent ledger order directly auditable.
    balance_after_sc: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    resident_slug: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Caller-supplied operation identity.  UUID fallback keeps every ordinary
    # flow auditable; stable keys make retryable transfers idempotent.
    ref_key: Mapped[str] = mapped_column(
        String(200), nullable=False, unique=True,
        default=lambda: str(uuid.uuid4()),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
