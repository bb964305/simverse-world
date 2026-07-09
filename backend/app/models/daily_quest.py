import uuid
from datetime import datetime, date as date_type, UTC

from sqlalchemy import String, Integer, Date, DateTime, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DailyQuest(Base):
    """A per-day topic quest (D3). One per user per day (UniqueConstraint)."""

    __tablename__ = "daily_quests"
    __table_args__ = (UniqueConstraint("user_id", "date", name="uq_daily_quest_user_date"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, index=True)
    date: Mapped[date_type] = mapped_column(Date, index=True)
    quest_json: Mapped[dict] = mapped_column(JSON)  # {resident_slug, resident_name, topic, min_turns}
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | done
    reward_sc: Mapped[int] = mapped_column(Integer, default=15)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
