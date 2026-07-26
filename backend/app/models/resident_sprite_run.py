"""Durable admin workflow for generated resident sprites."""
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ResidentSpriteRun(Base):
    __tablename__ = "resident_sprite_runs"
    __table_args__ = (UniqueConstraint("run_id", name="uq_resident_sprite_runs_run_id"),)

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    resident_id: Mapped[str] = mapped_column(
        String, ForeignKey("residents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="requested", index=True)
    direction_policy: Mapped[str] = mapped_column(String(32), nullable=False, default="mirror_right")
    generation_request_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    retry_of_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    capability_receipt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    manifest_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    candidate_texture_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    candidate_portrait_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    candidate_texture_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidate_portrait_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_texture_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_portrait_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)
    review_evidence_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    review_checklist_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_by: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_by: Mapped[str | None] = mapped_column(String, ForeignKey("users.id"), nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rollback_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_sprite_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    previous_portrait_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    previous_sprite_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    previous_sprite_generation_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
