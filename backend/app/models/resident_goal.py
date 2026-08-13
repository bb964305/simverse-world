import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Float, Text, DateTime, JSON, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ResidentGoal(Base):
    """A resident's life goal / story arc (A1)."""

    __tablename__ = "resident_goals"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    resident_id: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String(10), default="life")  # life | arc
    # Stable identity for goals installed from a code-owned template.  NULL is
    # deliberate for player/LLM-authored goals; PostgreSQL and SQLite both allow
    # multiple NULLs under the unique index below.  A preset arc keeps the same
    # key after it reaches ``achieved``, so a later bootstrap cannot replay it.
    template_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(String(200))
    motivation: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|achieved|failed|abandoned|superseded
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    milestones_json: Mapped[list] = mapped_column(JSON, default=list)  # [{title, done, note, at}]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("uq_resident_goals_template_key", "template_key", unique=True),
    )
