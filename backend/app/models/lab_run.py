import uuid
from datetime import datetime, UTC

from sqlalchemy import CheckConstraint, DateTime, Integer, JSON, String, Text, event, inspect
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.database import Base


class LabRun(Base):
    """One real sandbox execution session for a LabTask (spec §4.2).

    A task can retry → multiple runs. Money uses integer cents, never float.
    ``approvals_json`` is a *list* of pending sensitive-action approvals (a run
    may hit several). ``heartbeat_at`` is stamped by the Lab Runner so the
    watchdog can reap orphaned runs (crashed runner) and refund the escrow.
    """

    __tablename__ = "lab_runs"
    __table_args__ = (
        CheckConstraint(
            "protocol_version IN (1, 2)",
            name="ck_lab_runs_protocol_version",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    task_id: Mapped[str] = mapped_column(String, index=True)
    researcher_slug: Mapped[str] = mapped_column(String(100))
    adapter: Mapped[str] = mapped_column(String(20), default="mock")  # openclaw|hermes|computer_use|mock
    protocol_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)  # queued|running|succeeded|failed|cancelled|needs_approval
    scopes_json: Mapped[list] = mapped_column(JSON, default=list)  # effective scopes (≤ task.scopes)
    budget_usd_cents: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd_cents: Mapped[int] = mapped_column(Integer, default=0)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    approvals_json: Mapped[list | None] = mapped_column(JSON, nullable=True)  # list of pending approvals
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))

    @validates("protocol_version")
    def _validate_protocol_version(self, _key: str, value: int) -> int:
        if type(value) is not int or value not in (1, 2):
            raise ValueError("LabRun protocol_version must be integer 1 or 2")
        return value


@event.listens_for(LabRun, "before_update")
def _reject_protocol_version_update(_mapper, _connection, target: LabRun) -> None:
    if inspect(target).attrs.protocol_version.history.has_changes():
        raise ValueError("LabRun protocol_version is immutable after creation")


class LabRunStep(Base):
    """One step inside a run: think / tool_call / observation / message.

    Powers the frontend "live" stream and the audit trail. Everything written
    here is post-redaction (secrets/PII scrubbed) — spec §5.3.
    """

    __tablename__ = "lab_run_steps"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(String, index=True)
    seq: Mapped[int] = mapped_column(Integer)
    phase: Mapped[str] = mapped_column(String(20))  # think|tool_call|observation|message
    tool: Mapped[str | None] = mapped_column(String(60), nullable=True)  # e.g. "browser.navigate"
    summary: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
