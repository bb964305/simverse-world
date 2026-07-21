from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LabTerminalizationCommand(Base):
    """Durable, idempotent request consumed only by the Lab terminalizer."""

    __tablename__ = "lab_terminalization_commands"
    __table_args__ = (
        CheckConstraint("expected_epoch >= 0", name="ck_lab_terminalization_epoch"),
        CheckConstraint(
            "operation IN ('accept', 'auto_release', 'arbitrate_settle', "
            "'arbitrate_refund', 'fail', 'cancel', 'expire')",
            name="ck_lab_terminalization_command_operation",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_lab_terminalization_command_status",
        ),
        UniqueConstraint("idempotency_key", name="uq_lab_terminalization_idempotency"),
        UniqueConstraint(
            "operation",
            "task_id",
            "hold_id",
            "actor",
            "expected_epoch",
            name="uq_lab_terminalization_command_identity",
        ),
    )

    command_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation: Mapped[str] = mapped_column(String(24), index=True)
    task_id: Mapped[str] = mapped_column(String, index=True)
    hold_id: Mapped[str] = mapped_column(String, index=True)
    actor: Mapped[str] = mapped_column(String, index=True)
    expected_epoch: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LabTerminalizationReceipt(Base):
    """Immutable outcome used to converge exact command retries."""

    __tablename__ = "lab_terminalization_receipts"
    __table_args__ = (
        UniqueConstraint("command_id", name="uq_lab_terminalization_receipt_command"),
        UniqueConstraint("event_id", name="uq_lab_terminalization_receipt_event"),
        CheckConstraint("amount >= 0", name="ck_lab_terminalization_receipt_amount"),
        CheckConstraint(
            "journal_count >= 0",
            name="ck_lab_terminalization_receipt_journal_count",
        ),
        CheckConstraint(
            "length(result_digest) = 64",
            name="ck_lab_terminalization_receipt_digest",
        ),
    )

    receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    command_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("lab_terminalization_commands.command_id")
    )
    task_id: Mapped[str] = mapped_column(String, index=True)
    hold_id: Mapped[str] = mapped_column(String, index=True)
    operation: Mapped[str] = mapped_column(String(24))
    event_id: Mapped[str] = mapped_column(String(36))
    amount: Mapped[int] = mapped_column(Integer)
    journal_count: Mapped[int] = mapped_column(Integer)
    result_digest: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
