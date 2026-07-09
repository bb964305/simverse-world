import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Integer, DateTime, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LocationVisit(Base):
    """Per-user visit record for a named map location (S5 LocationTracker).

    Upserted by the location consumer: first insert = first visit (fires
    location_first_visit); subsequent entries bump visit_count.
    """

    __tablename__ = "location_visits"
    __table_args__ = (UniqueConstraint("user_id", "location_id", name="uq_location_visit"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, index=True)
    location_id: Mapped[str] = mapped_column(String(50))
    visit_count: Mapped[int] = mapped_column(Integer, default=1)
    first_visited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    last_visited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
