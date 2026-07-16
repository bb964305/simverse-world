import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Boolean, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class DynamicMechanic(Base):
    """World-overlay mechanic/lore added by an approved proposal (spec §4.6).

    ``kind`` ∈ quest_template | event | boosted_rule | lore | … and ``spec_json``
    carries the kind-specific payload (e.g. lore → {location_id, text}). Merged
    into the relevant runtime surface (location_lore, event templates, …) by the
    reload path.
    """

    __tablename__ = "dynamic_mechanics"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    code: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(30))
    spec_json: Mapped[dict] = mapped_column(JSON, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    proposal_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
