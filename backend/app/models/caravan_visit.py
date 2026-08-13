"""Durable caravan lifecycle and per-visit purchase audit rows."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    CheckConstraint, DateTime, ForeignKey, Integer, JSON, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


CARAVAN_PHASES = frozenset({
    "scheduled", "waiting", "inbound", "trading", "outbound",
    "departed", "cancelled",
})
CARAVAN_ACTIVE_PHASES = frozenset({
    "scheduled", "waiting", "inbound", "trading", "outbound",
})
CARAVAN_VISIBLE_PHASES = frozenset({"waiting", "inbound", "trading", "outbound"})


class CaravanVisit(Base):
    """One durable state-machine instance, keyed one-to-one to a market event."""

    __tablename__ = "caravan_visits"
    __table_args__ = (
        UniqueConstraint(
            "visibility_slot", name="uq_caravan_visits_visibility_slot"
        ),
        CheckConstraint(
            "phase IN ('scheduled','waiting','inbound','trading','outbound','departed','cancelled')",
            name="ck_caravan_visits_phase",
        ),
        CheckConstraint("version >= 1", name="ck_caravan_visits_version"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    world_event_id: Mapped[str] = mapped_column(
        String, ForeignKey("world_events.id", ondelete="CASCADE"), nullable=False,
        unique=True, index=True,
    )
    phase: Mapped[str] = mapped_column(
        String(20), nullable=False, default="scheduled", index=True
    )
    # Visible states claim singleton "world"; hidden and terminal rows use
    # NULL. This is the database backstop for concurrent lifecycle workers.
    visibility_slot: Mapped[str | None] = mapped_column(String(20), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_action_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    tile_x: Mapped[int] = mapped_column(Integer, nullable=False)
    tile_y: Mapped[int] = mapped_column(Integer, nullable=False)
    route_json: Mapped[list | None] = mapped_column(JSON, nullable=True)
    motion_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    motion_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Lease ownership is advisory until paired with ``version`` in a CAS UPDATE.
    # An expired owner can always be reclaimed after a worker restart.
    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    fee_sc: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fee_settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    imports_stocked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    imports_withdrawn_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    departed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    summary_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class CaravanVisitPurchase(Base):
    """A successfully committed external purchase; unique per item and visit."""

    __tablename__ = "caravan_visit_purchases"
    __table_args__ = (
        UniqueConstraint("visit_id", "item_code", name="uq_caravan_purchase_item"),
        CheckConstraint("qty > 0", name="ck_caravan_purchase_qty"),
        CheckConstraint(
            "gross_sc >= 0 AND tax_sc >= 0 AND net_sc >= 0 AND tax_sc + net_sc = gross_sc",
            name="ck_caravan_purchase_amounts",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    visit_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("caravan_visits.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    item_code: Mapped[str] = mapped_column(String(50), nullable=False)
    creator_slug: Mapped[str] = mapped_column(String(100), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    gross_sc: Mapped[int] = mapped_column(Integer, nullable=False)
    tax_sc: Mapped[int] = mapped_column(Integer, nullable=False)
    net_sc: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )


class CaravanMarketVisitor(Base):
    """One real resident assigned to one non-blocking market-hall stall slot.

    The row is both the restart-safe crowd assignment and, after arrival, the
    resident's durable import ownership receipt.  Keeping those two facts in a
    single row prevents a process restart from selecting a different crowd and
    gives the purchase path a natural per-visit/per-resident idempotency key.

    ``resident_id`` deliberately is a snapshot rather than a foreign key.  A
    later resident purge must not erase an already-audited money/stock movement.
    Creation still selects directly from the real ``residents`` table.
    """

    __tablename__ = "caravan_market_visitors"
    __table_args__ = (
        UniqueConstraint(
            "visit_id", "resident_id", name="uq_caravan_market_visitor_resident"
        ),
        UniqueConstraint(
            "visit_id", "slot_index", name="uq_caravan_market_visitor_slot"
        ),
        UniqueConstraint(
            "visit_id", "purchase_sequence",
            name="uq_caravan_market_visitor_purchase_sequence",
        ),
        CheckConstraint(
            "slot_index >= 0 AND slot_index < 4",
            name="ck_caravan_market_visitor_slot",
        ),
        CheckConstraint(
            "(item_code IS NULL AND spent_sc IS NULL AND purchase_sequence IS NULL "
            "AND purchased_at IS NULL) OR "
            "(item_code IS NOT NULL AND spent_sc > 0 AND purchase_sequence > 0 "
            "AND purchased_at IS NOT NULL)",
            name="ck_caravan_market_visitor_purchase",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    visit_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("caravan_visits.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    resident_id: Mapped[str] = mapped_column(String, nullable=False)
    resident_slug: Mapped[str] = mapped_column(String(100), nullable=False)
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    item_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    spent_sc: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purchase_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purchased_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
