"""S2-5 policies — typed, tiered, versioned world policy state.

Today's "policies" are untyped JSON blobs in ``system_config`` (``value`` is a
``String(2000)`` with no ``tier`` / ``procedure`` / ``version`` column, see
``app/models/system_config.py``). This table is the L2 政策层 upgrade
(SOCIETY_EXPANSION_PLAN §3.2): one row per policy key carrying

- ``tier``      — one of the four approval tiers (§3.3): ``administrative`` /
                  ``simple_majority`` / ``absolute_majority`` /
                  ``constitutional_core``;
- ``procedure`` — the routing label derived from the tier (``admin_direct`` /
                  ``civic_poll`` / ``civic_poll_supermajority`` / ``immutable``);
- ``version``   — the optimistic-concurrency field: every successful amend is a
                  conditional UPDATE ``WHERE key = :k AND version = :expected``
                  that bumps it by one (never a read-modify-write).

``value`` is ``Text`` on purpose — the 2000-char ceiling on
``system_config.value`` is the concrete reason this table exists.

The table is inert until ``POLIS_POLICY_ENABLED=true``: with the gate off no
business path reads or writes it and ``PolicyService`` falls back byte-for-byte
to ``ConfigService`` / ``system_config``.
"""
from datetime import datetime, UTC

from sqlalchemy import String, Integer, Text, DateTime, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Policy(Base):
    __tablename__ = "policies"
    __table_args__ = (
        UniqueConstraint("key", name="uq_policies_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    tier: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    procedure: Mapped[str] = mapped_column(String(64), nullable=False)
    group: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
