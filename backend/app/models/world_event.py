import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Text, Boolean, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WorldEvent(Base):
    """A time-boxed world event (S1 base bus): festival / weather / news / etc.

    A cron (app/tasks/event_cron.py) flips ``is_active`` as ``starts_at`` /
    ``ends_at`` pass and broadcasts the transition. Active events are injected
    into resident decision prompts and player-dialogue system prompts.

    ``created_by`` is a plain (un-constrained) user id column rather than a hard
    FK — following the same lesson as llm_usage: the value is pure provenance
    for admin-placed events, and skipping the FK dodges the insert-order /
    type-drift class of bugs that bit the real Postgres run on vm212.
    """

    __tablename__ = "world_events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    type: Mapped[str] = mapped_column(String(20))  # festival|weather|news|custom|script
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")  # text injected into prompts
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)  # type-specific data
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    created_by: Mapped[str | None] = mapped_column(String, nullable=True)  # admin user id (provenance)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
