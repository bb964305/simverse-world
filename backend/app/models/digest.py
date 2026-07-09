import uuid
from datetime import datetime, date as date_type, UTC

from sqlalchemy import String, Text, Date, DateTime, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Digest(Base):
    """A generated digest (A5 village daily report; E14 personal weekly later).

    ``user_id`` is "" for village-scope digests (not NULL) so the
    (scope, date, user_id) uniqueness — which makes nightly regeneration
    idempotent — holds on both sqlite and Postgres.
    """

    __tablename__ = "digests"
    __table_args__ = (UniqueConstraint("scope", "date", "user_id", name="uq_digest_scope_date_user"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    scope: Mapped[str] = mapped_column(String(20))  # "village" | "personal"
    date: Mapped[date_type] = mapped_column(Date, index=True)
    user_id: Mapped[str] = mapped_column(String, default="")  # "" for village
    title: Mapped[str] = mapped_column(String(200))
    content_md: Mapped[str] = mapped_column(Text, default="")
    stats_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
