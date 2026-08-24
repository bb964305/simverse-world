"""Audited, idempotent external-liquidity bootstrap batches."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EconomyBootstrapBatch(Base):
    __tablename__ = "economy_bootstrap_batches"
    __table_args__ = (
        UniqueConstraint("bootstrap_key", name="uq_economy_bootstrap_key"),
        CheckConstraint("resident_floor_sc >= 0", name="ck_economy_bootstrap_floor"),
        CheckConstraint("town_target_sc >= 0", name="ck_economy_bootstrap_town_target"),
        CheckConstraint("town_grant_sc >= 0", name="ck_economy_bootstrap_town_grant"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    bootstrap_key: Mapped[str] = mapped_column(String(100), nullable=False)
    requested_by_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    resident_floor_sc: Mapped[int] = mapped_column(Integer, nullable=False)
    town_target_sc: Mapped[int] = mapped_column(Integer, nullable=False)
    town_grant_sc: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class EconomyBootstrapGrant(Base):
    __tablename__ = "economy_bootstrap_grants"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "resident_slug", name="uq_economy_bootstrap_resident"
        ),
        CheckConstraint("amount_sc > 0", name="ck_economy_bootstrap_grant_amount"),
        CheckConstraint(
            "balance_before_sc >= 0 AND balance_after_sc >= balance_before_sc",
            name="ck_economy_bootstrap_grant_balances",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    batch_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("economy_bootstrap_batches.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    resident_slug: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    amount_sc: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_before_sc: Mapped[int] = mapped_column(Integer, nullable=False)
    balance_after_sc: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
