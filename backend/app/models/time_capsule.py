import uuid
from datetime import datetime, date as date_type, UTC

from sqlalchemy import String, Text, Date, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class TimeCapsule(Base):
    """A time-capsule letter a player entrusts to a resident (E7)."""

    __tablename__ = "time_capsules"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, index=True)
    carrier_resident_slug: Mapped[str] = mapped_column(String(100))
    deliver_on: Mapped[date_type] = mapped_column(Date, index=True)
    content: Mapped[str] = mapped_column(Text)
    resident_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="sealed")  # sealed | delivered
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
