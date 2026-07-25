"""S1-3 issue stances — bounded-confidence opinion scalar per (issue, resident).

``issue_key`` is a *denormalized free string* (normalized ``debate.topic`` /
``Poll.question``): the repo has no first-class issue entity, so there is no
issues registry and no FK — the same topic string recurring across debates /
polls reuses the same rows by design (KICKOFF S1-3 §1 现状缺口 / §2 任务 1).
"""

import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Integer, Float, DateTime, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class IssueStance(Base):
    """One resident's stance on one issue (Deffuant scalar in [-1, 1])."""

    __tablename__ = "issue_stances"
    __table_args__ = (
        # upsert conflict target — one row per (issue, resident), no dupes.
        UniqueConstraint("issue_key", "resident_slug", name="uq_issue_stance"),
        Index("ix_issue_stance_issue", "issue_key"),
        Index("ix_issue_stance_resident", "resident_slug"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    issue_key: Mapped[str] = mapped_column(String(300))
    resident_slug: Mapped[str] = mapped_column(String(100))
    stance: Mapped[float] = mapped_column(Float, default=0.0)        # ∈ [-1, 1]
    confidence: Mapped[float] = mapped_column(Float, default=0.5)    # ∈ [0, 1]
    updated_from: Mapped[str | None] = mapped_column(String(16), nullable=True)  # chat|debate|drift|seed
    interact_count: Mapped[int] = mapped_column(Integer, default=0)
    last_update_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
