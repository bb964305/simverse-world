"""Durable link between a Simverse resident and its on-chain Passport."""

from datetime import UTC, datetime
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Web3AgentPassport(Base):
    __tablename__ = "web3_agent_passports"
    __table_args__ = (
        UniqueConstraint("resident_id", name="uq_web3_agent_passport_resident"),
        UniqueConstraint(
            "chain_id", "registry_address", "agent_id",
            name="uq_web3_agent_passport_chain_agent",
        ),
        UniqueConstraint("registration_tx_hash", name="uq_web3_agent_passport_tx"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    resident_id: Mapped[str] = mapped_column(
        String, ForeignKey("residents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chain_id: Mapped[int] = mapped_column(Integer, nullable=False)
    registry_address: Mapped[str] = mapped_column(String(42), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(78), nullable=False)
    resident_key: Mapped[str] = mapped_column(String(66), nullable=False)
    registration_tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    metadata_uri: Mapped[str] = mapped_column(String(1000), nullable=False)
    metadata_hash: Mapped[str] = mapped_column(String(66), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
