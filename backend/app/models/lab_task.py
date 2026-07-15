import uuid
from datetime import datetime, timedelta, UTC

from sqlalchemy import String, Integer, Text, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _default_deadline() -> datetime:
    return datetime.now(UTC) + timedelta(hours=24)


class LabTask(Base):
    """A player-published real-world commission for a researcher (spec §4.1).

    State machine (optimistic, borrowing Commission's pattern):
        draft → funded → assigned → running → review
              → completed | rejected | failed | expired | cancelled

    Acceptance defaults to manual with a 72h auto-release (anti-runaway); the
    artifact stays locked until release (anti-freeload); reject-result is
    capped at 1 (then admin arbitration). Open recruitment
    (researcher_slug=None) is auto-dispatched by backend rules, not the tick.
    """

    __tablename__ = "lab_tasks"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    issuer_user_id: Mapped[str] = mapped_column(String, index=True)  # FK users.id (player)
    researcher_slug: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)  # None = open recruitment
    title: Mapped[str] = mapped_column(String(200))
    brief_md: Mapped[str] = mapped_column(Text, default="")
    scopes_json: Mapped[list] = mapped_column(JSON, default=list)  # ["web_search","browse","code",...]
    reward_sc: Mapped[int] = mapped_column(Integer, default=0)
    platform_fee_sc: Mapped[int] = mapped_column(Integer, default=0)
    deliverable_kind: Mapped[str] = mapped_column(String(20), default="report")  # report|file|link|dataset|world_change
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    hold_id: Mapped[str | None] = mapped_column(String, nullable=True)
    accepted_run_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reject_count: Mapped[int] = mapped_column(Integer, default=0)
    result_summary_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_default_deadline)
    review_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # 72h auto-release
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
