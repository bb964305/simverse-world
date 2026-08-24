"""Durable player market receipts and reviewed Lab-to-market candidates."""
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


class CaravanMarketPurchase(Base):
    """One player purchase inside one durable caravan visit.

    ``request_key`` makes transport retries safe while the visit/user/offer
    uniqueness enforces the first release's one-per-visit product contract.
    The ordinary ``purchases`` row remains the player's inventory receipt.
    """

    __tablename__ = "caravan_market_purchases"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "request_key", name="uq_market_purchase_user_request"
        ),
        UniqueConstraint(
            "visit_id", "user_id", "offer_code",
            name="uq_market_purchase_visit_user_offer",
        ),
        CheckConstraint("qty > 0", name="ck_market_purchase_qty"),
        CheckConstraint("total_sc >= 0", name="ck_market_purchase_total"),
        CheckConstraint(
            "offer_type IN ('good','service','contract')",
            name="ck_market_purchase_offer_type",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    visit_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("caravan_visits.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
        index=True,
    )
    request_key: Mapped[str] = mapped_column(String(64), nullable=False)
    offer_code: Mapped[str] = mapped_column(String(80), nullable=False)
    offer_type: Mapped[str] = mapped_column(String(20), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    total_sc: Mapped[int] = mapped_column(Integer, nullable=False)
    effect_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class LabMarketCandidate(Base):
    """A released Lab artifact nominated for a curated market offer.

    Approval places the artifact in the next-visit candidate pool. It never
    grants an artifact permission to mutate the world or create an executable
    effect; publication remains a separate, allowlisted product step.
    """

    __tablename__ = "lab_market_candidates"
    __table_args__ = (
        UniqueConstraint("artifact_id", name="uq_lab_market_candidate_artifact"),
        CheckConstraint(
            "status IN ('pending','approved','rejected','published')",
            name="ck_lab_market_candidate_status",
        ),
        CheckConstraint(
            "offer_type IN ('good','service','contract')",
            name="ck_lab_market_candidate_offer_type",
        ),
        CheckConstraint("suggested_price_sc >= 0", name="ck_lab_market_candidate_price"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    artifact_id: Mapped[str] = mapped_column(
        String, ForeignKey("lab_artifacts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    task_id: Mapped[str] = mapped_column(
        String, ForeignKey("lab_tasks.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    proposed_by_user_id: Mapped[str] = mapped_column(
        String, ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    offer_type: Mapped[str] = mapped_column(String(20), nullable=False)
    suggested_price_sc: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", index=True
    )
    reviewed_by_user_id: Mapped[str | None] = mapped_column(
        String, ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    review_note: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
