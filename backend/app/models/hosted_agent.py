"""Durable controllers and turn journals for hosted Agent players."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


HOSTED_AGENT_DESIRED_STATUSES = frozenset({"running", "paused", "disabled"})
HOSTED_AGENT_RUNTIME_STATUSES = frozenset(
    {
        "provisioning",
        "idle",
        "claimed",
        "backoff",
        "budget_paused",
        "auth_blocked",
        "error",
        "disabled",
    }
)
HOSTED_AGENT_TERMINAL_DESIRED_STATUSES = frozenset({"disabled"})


class HostedAgentController(Base):
    """One restart-safe hosted controller for one Agent Player identity.

    Provider credentials and the Agent play credential are stored only inside
    the authenticated ``secret_ciphertext`` envelope.  Cleartext columns are
    deliberately limited to fields safe for the admin status surface.
    """

    __tablename__ = "hosted_agent_controllers"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "request_id", name="uq_hosted_agent_owner_request"
        ),
        UniqueConstraint("capacity_slot", name="uq_hosted_agent_capacity_slot"),
        CheckConstraint(
            "desired_status IN ('running','paused','disabled')",
            name="ck_hosted_agent_desired_status",
        ),
        CheckConstraint(
            "runtime_status IN "
            "('provisioning','idle','claimed','backoff','budget_paused',"
            "'auth_blocked','error','disabled')",
            name="ck_hosted_agent_runtime_status",
        ),
        CheckConstraint("control_version >= 1", name="ck_hosted_agent_control_version"),
        CheckConstraint("lease_epoch >= 0", name="ck_hosted_agent_lease_epoch"),
        CheckConstraint("turn_sequence >= 0", name="ck_hosted_agent_turn_sequence"),
        CheckConstraint(
            "capacity_slot IS NULL OR capacity_slot >= 0",
            name="ck_hosted_agent_capacity_slot",
        ),
        CheckConstraint(
            "heartbeat_seconds >= 15 AND heartbeat_seconds <= 60",
            name="ck_hosted_agent_heartbeat_seconds",
        ),
        CheckConstraint(
            "action_interval_seconds >= 5 AND action_interval_seconds <= 3600",
            name="ck_hosted_agent_action_interval_seconds",
        ),
        CheckConstraint(
            "max_actions_per_day >= 1 AND max_actions_per_day <= 1000",
            name="ck_hosted_agent_daily_actions",
        ),
        CheckConstraint(
            "max_provider_calls_per_day >= 1 AND max_provider_calls_per_day <= 2000",
            name="ck_hosted_agent_daily_calls",
        ),
        CheckConstraint(
            "max_tokens_per_day >= 1000 AND max_tokens_per_day <= 10000000",
            name="ck_hosted_agent_daily_tokens",
        ),
        CheckConstraint(
            "max_output_tokens >= 1 AND max_output_tokens <= 2000",
            name="ck_hosted_agent_output_tokens",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    owner_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), nullable=False, index=True
    )
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    create_request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_player_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("agent_players.id"), nullable=True, unique=True, index=True
    )
    desired_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="running", index=True
    )
    runtime_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="provisioning", index=True
    )
    control_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    capacity_slot: Mapped[int | None] = mapped_column(Integer, nullable=True)

    provider_host: Mapped[str] = mapped_column(String(255), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_validation_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    secret_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    secret_envelope: Mapped[str] = mapped_column(Text, nullable=False)

    identity_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    policy_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    heartbeat_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    action_interval_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30
    )
    max_actions_per_day: Mapped[int] = mapped_column(
        Integer, nullable=False, default=200
    )
    max_provider_calls_per_day: Mapped[int] = mapped_column(
        Integer, nullable=False, default=400
    )
    max_tokens_per_day: Mapped[int] = mapped_column(
        Integer, nullable=False, default=200_000
    )
    max_output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=600
    )

    next_tick_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True
    )
    next_action_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True
    )
    provider_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_presence_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_action_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    turn_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class HostedAgentTurn(Base):
    """Encrypted, recoverable journal for one observe/decide/action cycle."""

    __tablename__ = "hosted_agent_turns"
    __table_args__ = (
        UniqueConstraint(
            "controller_id", "sequence", name="uq_hosted_agent_turn_sequence"
        ),
        UniqueConstraint(
            "controller_id", "action_id", name="uq_hosted_agent_turn_action"
        ),
        CheckConstraint(
            "state IN ('observed','budget_reserved','calling','decision_ready',"
            "'committing','completed','abandoned','failed')",
            name="ck_hosted_agent_turn_state",
        ),
        CheckConstraint("sequence >= 1", name="ck_hosted_agent_turn_sequence_positive"),
        CheckConstraint("lease_epoch >= 0", name="ck_hosted_agent_turn_lease_epoch"),
        CheckConstraint("control_version >= 1", name="ck_hosted_agent_turn_control_version"),
        CheckConstraint("reserved_tokens >= 0", name="ck_hosted_agent_turn_reserved_tokens"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    controller_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("hosted_agent_controllers.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    lease_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    control_version: Mapped[int] = mapped_column(Integer, nullable=False)
    observation_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    event_cursor: Mapped[int | None] = mapped_column(Integer, nullable=True)

    observation_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    observation_envelope: Mapped[str] = mapped_column(Text, nullable=False)
    decision_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decision_envelope: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result_envelope: Mapped[str | None] = mapped_column(Text, nullable=True)

    action_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    public_summary: Mapped[str | None] = mapped_column(String(280), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    budget_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    reserved_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    journaled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class HostedAgentDailyUsage(Base):
    """Hard per-controller UTC daily counters, including pre-call reservations."""

    __tablename__ = "hosted_agent_daily_usage"
    __table_args__ = (
        CheckConstraint(
            "calls_reserved >= 0 AND calls_charged >= 0 AND actions >= 0 AND "
            "tokens_reserved >= 0 AND tokens_charged >= 0 AND input_tokens >= 0 "
            "AND output_tokens >= 0",
            name="ck_hosted_agent_usage_nonnegative",
        ),
    )

    controller_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("hosted_agent_controllers.id", ondelete="CASCADE"),
        primary_key=True,
    )
    usage_date: Mapped[date] = mapped_column(Date, primary_key=True)
    calls_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    calls_charged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_charged: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
