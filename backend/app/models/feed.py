import uuid
from datetime import datetime, UTC

from sqlalchemy import String, DateTime, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Follow(Base):
    """A player following a resident (E11)."""

    __tablename__ = "follows"
    __table_args__ = (UniqueConstraint("user_id", "resident_slug", name="uq_follow_user_resident"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, index=True)
    resident_slug: Mapped[str] = mapped_column(String(100), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))


class FeedEvent(Base):
    """A notable resident event surfaced to followers (E11)."""

    __tablename__ = "feed_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    resident_slug: Mapped[str] = mapped_column(String(100), index=True)
    # kind: goal_achieved | goal_milestone | personality_shift | creation | debate | mood_swing
    kind: Mapped[str] = mapped_column(String(30))
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True)
