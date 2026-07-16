import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Integer, Text, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WorldChangeProposal(Base):
    """A structured, human-reviewed change to the real game world (spec §4.5).

    Produced by a successful LabRun (or a resident/admin), fueled by the
    researcher's treasury (cost_sc frozen on create, consumed on apply, refunded
    on reject). Never auto-applied — Apply always goes through admin review.
    """

    __tablename__ = "world_change_proposals"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    origin: Mapped[str] = mapped_column(String(20), default="lab_run")  # lab_run|resident|admin
    origin_ref: Mapped[str | None] = mapped_column(String, nullable=True)  # run_id / resident_slug
    author_slug: Mapped[str | None] = mapped_column(String(100), nullable=True)
    kind: Mapped[str] = mapped_column(String(30))  # add_location|add_mechanic|add_lore|edit_location|add_npc|edit_npc
    title: Mapped[str] = mapped_column(String(200))
    rationale_md: Mapped[str] = mapped_column(Text, default="")
    patch_json: Mapped[dict] = mapped_column(JSON, default=dict)
    cost_sc: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending→approved→applied|rejected|reverted|failed
    risk_level: Mapped[str] = mapped_column(String(10), default="low")  # low|medium|high
    reviewer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
