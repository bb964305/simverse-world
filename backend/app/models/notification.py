import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Text, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Notification(Base):
    """A per-user notification (S4 notification center).

    Written durably so notifications produced while the user is offline can be
    pulled on next login; if the user is online, notification_service also pushes
    a live WS ``notification`` message. ``user_id`` is a plain indexed column
    (not a hard FK) — same telemetry-table lesson as llm_usage / world_events.
    """

    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String, index=True)
    # kind: resident_greeting | achievement | capsule_delivered | commission | feed | system
    kind: Mapped[str] = mapped_column(String(30))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
