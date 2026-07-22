"""Durable Gateway truth for Lab Runtime protocol-v2 execution."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


def _authority_epoch_default(context) -> int:
    return int(context.get_current_parameters().get("fencing_epoch") or 0)


class LabRuntimeSession(Base):
    __tablename__ = "lab_runtime_sessions"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_lab_runtime_sessions_run"),
        UniqueConstraint(
            "client_run_id",
            "fencing_epoch",
            name="uq_lab_runtime_sessions_client_epoch",
        ),
        UniqueConstraint(
            "id", "fencing_epoch", name="uq_lab_runtime_sessions_id_epoch"
        ),
        CheckConstraint(
            "status IN ('creating','ready','completed','failed','fenced',"
            "'cancelled','quarantined')",
            name="ck_lab_runtime_sessions_status",
        ),
        CheckConstraint(
            "protocol_version = 2", name="ck_lab_runtime_sessions_protocol"
        ),
        CheckConstraint(
            "durability_class IN ('session_affine')",
            name="ck_lab_runtime_sessions_durability",
        ),
        CheckConstraint(
            "fencing_epoch >= 0",
            name="ck_lab_runtime_sessions_epoch",
        ),
        CheckConstraint(
            "authority_epoch >= fencing_epoch",
            name="ck_lab_runtime_sessions_authority_epoch",
        ),
        CheckConstraint(
            "provider_cursor_committed >= 0 AND provider_cursor_acked >= 0 "
            "AND provider_cursor_acked <= provider_cursor_committed",
            name="ck_lab_runtime_sessions_cursors",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(
        String, ForeignKey("lab_runs.id"), nullable=False, index=True
    )
    client_run_id: Mapped[str] = mapped_column(String(80), nullable=False)
    fencing_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    authority_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=_authority_epoch_default
    )
    protocol_version: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    provider_name: Mapped[str] = mapped_column(String(80), nullable=False)
    provider_session_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    locator_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    durability_class: Mapped[str] = mapped_column(
        String(30), nullable=False, default="session_affine"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="creating", index=True
    )
    creation_owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    creation_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_cursor_committed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    provider_cursor_acked: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class LabRuntimeTurn(Base):
    __tablename__ = "lab_runtime_turns"
    __table_args__ = (
        UniqueConstraint(
            "id", "session_id", name="uq_lab_runtime_turns_id_session"
        ),
        UniqueConstraint(
            "session_id", "turn_id", name="uq_lab_runtime_turns_session_turn"
        ),
        UniqueConstraint(
            "session_id", "sequence", name="uq_lab_runtime_turns_session_sequence"
        ),
        CheckConstraint("sequence >= 0", name="ck_lab_runtime_turns_sequence"),
        CheckConstraint(
            "status IN ('ready','intent_pending','result_recorded','runtime_acked',"
            "'completed','final','failed')",
            name="ck_lab_runtime_turns_status",
        ),
        CheckConstraint(
            "provider_cursor >= 0", name="ck_lab_runtime_turns_provider_cursor"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lab_runtime_sessions.id"), nullable=False, index=True
    )
    turn_id: Mapped[str] = mapped_column(String(100), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ready", index=True
    )
    provider_cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    final_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class LabRuntimeIntent(Base):
    __tablename__ = "lab_runtime_intents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["runtime_turn_id", "session_id"],
            ["lab_runtime_turns.id", "lab_runtime_turns.session_id"],
            name="fk_lab_runtime_intents_turn_session",
        ),
        ForeignKeyConstraint(
            ["session_id", "fencing_epoch"],
            ["lab_runtime_sessions.id", "lab_runtime_sessions.fencing_epoch"],
            name="fk_lab_runtime_intents_session_epoch",
        ),
        UniqueConstraint(
            "session_id", "intent_id", name="uq_lab_runtime_intents_session_intent"
        ),
        UniqueConstraint("action_id", name="uq_lab_runtime_intents_action"),
        UniqueConstraint(
            "id",
            "session_id",
            "runtime_turn_id",
            "intent_id",
            "action_id",
            "fencing_epoch",
            name="uq_lab_runtime_intents_result_binding",
        ),
        CheckConstraint(
            "status IN ('pending','result_recorded','runtime_acked','cancelled','failed')",
            name="ck_lab_runtime_intents_status",
        ),
        CheckConstraint(
            "provider_cursor >= 0", name="ck_lab_runtime_intents_provider_cursor"
        ),
        CheckConstraint("fencing_epoch >= 0", name="ck_lab_runtime_intents_epoch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    runtime_turn_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True
    )
    intent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    action_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tool_name: Mapped[str] = mapped_column(String(120), nullable=False)
    args_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    args_redacted_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", index=True
    )
    provider_cursor: Mapped[int] = mapped_column(Integer, nullable=False)
    fencing_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now, onupdate=_now
    )


class LabRuntimeResult(Base):
    __tablename__ = "lab_runtime_results"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "runtime_intent_id",
                "session_id",
                "runtime_turn_id",
                "intent_id",
                "action_id",
                "fencing_epoch",
            ],
            [
                "lab_runtime_intents.id",
                "lab_runtime_intents.session_id",
                "lab_runtime_intents.runtime_turn_id",
                "lab_runtime_intents.intent_id",
                "lab_runtime_intents.action_id",
                "lab_runtime_intents.fencing_epoch",
            ],
            name="fk_lab_runtime_results_intent_binding",
        ),
        UniqueConstraint("command_id", name="uq_lab_runtime_results_command"),
        UniqueConstraint("receipt_id", name="uq_lab_runtime_results_receipt"),
        UniqueConstraint(
            "runtime_intent_id", name="uq_lab_runtime_results_intent_row"
        ),
        UniqueConstraint(
            "session_id", "intent_id", name="uq_lab_runtime_results_session_intent"
        ),
        CheckConstraint(
            "outcome IN ('succeeded','denied','failed')",
            name="ck_lab_runtime_results_outcome",
        ),
        CheckConstraint(
            "(receipt_id IS NULL AND runtime_acked_at IS NULL) OR "
            "(receipt_id IS NOT NULL AND runtime_acked_at IS NOT NULL)",
            name="ck_lab_runtime_results_receipt_ack_pair",
        ),
        CheckConstraint("fencing_epoch >= 0", name="ck_lab_runtime_results_epoch"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    runtime_turn_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    runtime_intent_id: Mapped[str] = mapped_column(String(36), nullable=False)
    intent_id: Mapped[str] = mapped_column(String(100), nullable=False)
    action_id: Mapped[str] = mapped_column(String(100), nullable=False)
    command_id: Mapped[str] = mapped_column(String(100), nullable=False)
    # Populated only after the authenticated Runtime accepts this exact command.
    # NULL is the durable "persisted but not yet delivered" state.
    receipt_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    result_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    fencing_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    runtime_acked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_now
    )
