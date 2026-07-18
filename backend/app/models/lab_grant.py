"""Signed capability grants issued to a Lab run's agent (Grant/Policy/Broker
boundary, PRD §Protocols). A grant is the durable, revocable backing store the
Policy Engine and Tool Broker check every tool call against — depth 0 is a
run's top-level agent, depth 1 is at most one delegated sub-agent (see
``app.lab.protocol.GrantClaims``, which this table's columns mirror 1:1).
"""
import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Integer, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LabCapabilityGrant(Base):
    __tablename__ = "lab_capability_grants"

    jti: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id: Mapped[str] = mapped_column(String, index=True)
    task_id: Mapped[str] = mapped_column(String)
    run_id: Mapped[str] = mapped_column(String, index=True)
    agent_id: Mapped[str] = mapped_column(String(60))
    parent_jti: Mapped[str | None] = mapped_column(String, nullable=True)
    depth: Mapped[int] = mapped_column(Integer, default=0)
    audience: Mapped[str] = mapped_column(String(30), default="tool-broker")
    capabilities_json: Mapped[list] = mapped_column(JSON, default=list)
    resources_json: Mapped[dict] = mapped_column(JSON, default=dict)
    egress_json: Mapped[list] = mapped_column(JSON, default=list)
    budgets_json: Mapped[dict] = mapped_column(JSON, default=dict)
    policy_version: Mapped[str] = mapped_column(String(20))
    fencing_epoch: Mapped[int] = mapped_column(Integer, default=0)
    nbf: Mapped[int] = mapped_column(Integer)
    exp: Mapped[int] = mapped_column(Integer)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grant_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
