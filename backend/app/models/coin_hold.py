import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CoinHold(Base):
    """Escrow ledger row (spec §4.4).

    Funding a LabTask calls ``coin_service.hold`` which debits the issuer's
    balance (a real -amount Transaction, so the money leaves circulation) and
    records this row as "nominally owned by a task, not yet distributed".
    Settlement splits the amount into rewards/treasury/sink; refund returns it
    all to the issuer. Invariant enforced in coin_service.settle:
    sum(splits) == amount.
    """

    __tablename__ = "coin_holds"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, index=True)  # frozen party (issuer)
    amount: Mapped[int] = mapped_column(Integer)             # reward + platform fee
    reason: Mapped[str] = mapped_column(String(100))          # e.g. "lab_task:<id>"
    status: Mapped[str] = mapped_column(String(20), default="held", index=True)  # held|settled|refunded
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
