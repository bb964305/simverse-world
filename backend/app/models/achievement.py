import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Text, Integer, Boolean, DateTime, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Achievement(Base):
    """Achievement definition (S2). Seeded from in-code defs; the table exists so
    ops can edit copy/rewards without a deploy. The engine's source of truth is
    app/events/achievements.py::ACHIEVEMENT_DEFS."""

    __tablename__ = "achievements"

    code: Mapped[str] = mapped_column(String(50), primary_key=True)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str] = mapped_column(String(20), default="🏆")
    points: Mapped[int] = mapped_column(Integer, default=0)   # season score weight (E12)
    reward_sc: Mapped[int] = mapped_column(Integer, default=0)
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)


class UserAchievement(Base):
    __tablename__ = "user_achievements"
    __table_args__ = (UniqueConstraint("user_id", "code", name="uq_user_achievement"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, index=True)
    code: Mapped[str] = mapped_column(String(50))
    progress_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {"count":7,"target":10}
    unlocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
