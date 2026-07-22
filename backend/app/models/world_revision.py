"""Immutable audit trail of applied world-state changes, keyed by the
``WorldChangeProposal`` that authorized them (Thin D boundary — the World
Governor owns all world effects, PRD §Data and API Evolution).
``base_revision_id`` chains to the prior revision for the same
``location_slug`` (optimistic-concurrency revert support — later task).
"""
import uuid
from datetime import datetime, UTC

from sqlalchemy import String, DateTime, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WorldRevision(Base):
    """status: applied | reverted. change_kind (v1): add_lore | edit_location."""

    __tablename__ = "world_revisions"
    __table_args__ = (
        UniqueConstraint("proposal_id", name="uq_world_revisions_proposal"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String)
    proposal_id: Mapped[str] = mapped_column(String, index=True)
    location_slug: Mapped[str] = mapped_column(String(100), index=True)
    change_kind: Mapped[str] = mapped_column(String(30))
    base_revision_id: Mapped[str | None] = mapped_column(String, nullable=True)
    before_state_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after_state_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="applied")
    applied_by: Mapped[str | None] = mapped_column(String, nullable=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    reverted_by: Mapped[str | None] = mapped_column(String, nullable=True)
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
