import uuid
from datetime import datetime, timedelta, UTC

from sqlalchemy import String, Integer, Text, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _default_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(hours=48)


class Commission(Base):
    """A resident-issued errand a player can accept (B1)."""

    __tablename__ = "commissions"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    issuer_resident_id: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String(30))  # deliver_message | chat_topic | visit_location
    title: Mapped[str] = mapped_column(String(200))
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    reward_sc: Mapped[int] = mapped_column(Integer, default=20)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)  # open|accepted|completed|expired
    acceptor_user_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_default_expiry)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
