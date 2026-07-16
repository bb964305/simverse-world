import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Boolean, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DynamicLocation(Base):
    """World-overlay location added by an approved proposal (spec §4.6).

    ``data_json`` is isomorphic to a LOCATIONS entry (name/type/bounds/center/
    entrance/…). ``map_data.load_dynamic_locations`` merges the active rows into
    the in-memory LOCATIONS at startup / on the reload signal, so an approved
    building becomes reachable (pathfinding/planning/codex) without a redeploy.
    """

    __tablename__ = "dynamic_locations"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    slug: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    data_json: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    proposal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
