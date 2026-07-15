import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Text, DateTime, JSON
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
