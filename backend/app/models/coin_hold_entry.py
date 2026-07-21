import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CoinHoldEntry(Base):
    """Append-only distribution journal for one escrow terminal action."""

    __tablename__ = "coin_hold_entries"
    __table_args__ = (
        UniqueConstraint("operation_key", name="uq_coin_hold_entries_operation_key"),
        UniqueConstraint(
            "hold_id",
            "terminal_action",
            "recipient_key",
            name="uq_coin_hold_entries_terminal_recipient",
        ),
        CheckConstraint("amount > 0", name="ck_coin_hold_entries_amount_positive"),
        CheckConstraint(
            "terminal_action IN ('settle', 'refund')",
            name="ck_coin_hold_entries_terminal_action",
        ),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    hold_id: Mapped[str] = mapped_column(
        String, ForeignKey("coin_holds.id"), index=True
    )
    terminal_action: Mapped[str] = mapped_column(String(20))
    recipient_key: Mapped[str] = mapped_column(String(160))
    amount: Mapped[int] = mapped_column(Integer)
    operation_key: Mapped[str] = mapped_column(String(320))
    reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
