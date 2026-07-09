import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Integer, Text, DateTime, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Season(Base):
    """A themed season (C3/E12)."""

    __tablename__ = "seasons"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    title: Mapped[str] = mapped_column(String(200))
    theme: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="voting")  # voting|active|settled
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)  # world-view patch + final_ranks/settled


class SeasonScript(Base):
    """One act of a season script (C3)."""

    __tablename__ = "season_scripts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    season_id: Mapped[str] = mapped_column(String, index=True)
    act: Mapped[int] = mapped_column(Integer, default=1)
    trigger_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|fired


class Poll(Base):
    __tablename__ = "polls"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    season_id: Mapped[str | None] = mapped_column(String, nullable=True)
    question: Mapped[str] = mapped_column(String(300))
    options_json: Mapped[list] = mapped_column(JSON, default=list)
    closes_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|closed


class Vote(Base):
    __tablename__ = "votes"
    __table_args__ = (UniqueConstraint("poll_id", "user_id", name="uq_vote_poll_user"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    poll_id: Mapped[str] = mapped_column(String, index=True)
    user_id: Mapped[str] = mapped_column(String)
    option_idx: Mapped[int] = mapped_column(Integer)


class SeasonScore(Base):
    __tablename__ = "season_scores"
    __table_args__ = (UniqueConstraint("season_id", "user_id", name="uq_season_score"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    season_id: Mapped[str] = mapped_column(String, index=True)
    user_id: Mapped[str] = mapped_column(String, index=True)
    points: Mapped[int] = mapped_column(Integer, default=0)
    breakdown_json: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
