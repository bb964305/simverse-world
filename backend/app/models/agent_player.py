"""External Agent player identities and their revocable opaque credentials."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AgentPlayer(Base):
    """A non-human principal controlling one ordinary ``player`` resident."""

    __tablename__ = "agent_players"

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id"), nullable=False, unique=True
    )
    resident_id: Mapped[str] = mapped_column(
        String, ForeignKey("residents.id"), nullable=False, unique=True
    )
    control_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="external_agent"
    )
    model_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    client_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    role_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    public_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    observation_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_seen_event_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    # Durable cross-endpoint reservation used while a billable Agent operation
    # runs outside a DB transaction. It prevents parallel actions/turn IDs from
    # exploiting the same observation and fanning out unpaid model calls.
    operation_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    operation_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operation_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class AgentCredential(Base):
    """Hashed opaque pairing/play/view credential for an Agent player.

    Plaintext credentials are returned exactly when created and never stored.
    """

    __tablename__ = "agent_credentials"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_agent_credentials_token_hash"),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    agent_player_id: Mapped[str] = mapped_column(
        String, ForeignKey("agent_players.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentActionReceipt(Base):
    """Durable idempotency receipt for one Agent action request."""

    __tablename__ = "agent_action_receipts"
    __table_args__ = (
        UniqueConstraint(
            "agent_player_id", "action_id", name="uq_agent_action_agent_action_id"
        ),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    agent_player_id: Mapped[str] = mapped_column(
        String, ForeignKey("agent_players.id"), nullable=False, index=True
    )
    action_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action_type: Mapped[str] = mapped_column(String(32), nullable=False)
    observation_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class AgentNpcChatTurnReceipt(Base):
    """Durable idempotency and replay envelope for one Agent->NPC chat turn."""

    __tablename__ = "agent_npc_chat_turn_receipts"
    __table_args__ = (
        UniqueConstraint(
            "agent_player_id",
            "turn_id",
            name="uq_agent_npc_chat_turn_agent_turn_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    agent_player_id: Mapped[str] = mapped_column(
        String, ForeignKey("agent_players.id"), nullable=False, index=True
    )
    resident_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("residents.id"), nullable=True, index=True
    )
    conversation_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("conversations.id"), nullable=True, index=True
    )
    turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    observation_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Minimal retry metadata only (currently the optional public scene context).
    # Never persist the assembled system prompt: it can contain private memories
    # and other context that does not belong in an idempotency receipt.
    recovery_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    lease_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class AgentEvent(Base):
    """Durable private inbox events delivered to one Agent player."""

    __tablename__ = "agent_events"
    __table_args__ = (
        UniqueConstraint(
            "agent_player_id", "sequence", name="uq_agent_events_agent_sequence"
        ),
    )

    id: Mapped[str] = mapped_column(
        String, primary_key=True, default=lambda: str(uuid.uuid4())
    )
    agent_player_id: Mapped[str] = mapped_column(
        String, ForeignKey("agent_players.id"), nullable=False, index=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
