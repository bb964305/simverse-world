import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Integer, DateTime, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Debate(Base):
    """A resident debate with player staking + voting (E9).

    votes_a/votes_b are a deviation from the spec (which omits a debate_votes
    table): free-vote counters on the row, with per-user dedup in Redis.
    """

    __tablename__ = "debates"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    topic: Mapped[str] = mapped_column(String(300))
    resident_a_slug: Mapped[str] = mapped_column(String(100))
    resident_b_slug: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="announced")  # announced|live|voting|settled
    transcript_json: Mapped[list] = mapped_column(JSON, default=list)
    winner: Mapped[str | None] = mapped_column(String(10), nullable=True)  # a|b|draw
    pool_a: Mapped[int] = mapped_column(Integer, default=0)
    pool_b: Mapped[int] = mapped_column(Integer, default=0)
    votes_a: Mapped[int] = mapped_column(Integer, default=0)
    votes_b: Mapped[int] = mapped_column(Integer, default=0)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DebateStake(Base):
    __tablename__ = "debate_stakes"
    __table_args__ = (UniqueConstraint("debate_id", "user_id", name="uq_debate_stake"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    debate_id: Mapped[str] = mapped_column(String, index=True)
    user_id: Mapped[str] = mapped_column(String)
    side: Mapped[str] = mapped_column(String(10))  # a|b
    amount: Mapped[int] = mapped_column(Integer)
    payout: Mapped[int | None] = mapped_column(Integer, nullable=True)
