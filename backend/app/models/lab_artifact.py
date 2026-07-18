import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Integer, Text, DateTime, JSON, Boolean
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LabArtifact(Base):
    """A deliverable produced by a run (spec §4.3).

    Locked until the task is released (accepted or auto-released) — the router
    gates ``GET /lab/artifacts/{id}`` on task completion to prevent freeloading.
    External links are shown with the full URL + a warning and are never
    auto-followed (indirect prompt-injection defense, spec §5.3).
    """

    __tablename__ = "lab_artifacts"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(String, index=True)
    task_id: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String(20), default="text")  # file|link|text|image|dataset
    title: Mapped[str] = mapped_column(String(200), default="")
    uri: Mapped[str | None] = mapped_column(String(1000), nullable=True)  # storage path or external link
    text_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    meta_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    # --- V12 integrity + retention (P2-B; DB-slice, no object store — see
    # .superpowers/sdd/task-9-brief.md). All nullable/defaulted so existing
    # (legacy, flag-off) rows keep working unchanged. ---
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)  # text_md's or uri's utf-8 digest
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    producer_action_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    provenance: Mapped[str] = mapped_column(String(30), default="runtime")  # runtime|verifier|system
    scan_status: Mapped[str] = mapped_column(String(20), default="skipped")  # skipped|pending|clean|flagged
    verification_status: Mapped[str] = mapped_column(String(20), default="unverified")  # unverified|verified|rejected
    retention_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
