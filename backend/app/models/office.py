"""S2-1 offices — the unified job/office table (职位实体化).

One row per office_key (mayor / town_clerk / postman / doctor, extensible to
lab_director), UNIQUE on office_key: the single-holder assumption matches
today's ``find_duty_resident`` first-match semantics. A vacant office keeps
its row with ``holder_slug = NULL`` (never deleted) so occupancy/vacancy is
observable over time.

``perms_json`` (S2-2 mayor discretion consumes) and ``fill_strategy``
(S3-1 sortition consumes) are interface-surface only in S2-1 — populated,
not yet acted on.

``term_ends_at`` NULL = unlimited term (byte-compatible with today's
overwrite-on-reelection mayor). Term arithmetic goes through
``app/world_clock.py`` exclusively; values are stored as UTC-aware datetimes.
"""
from datetime import datetime, UTC

from sqlalchemy import String, Integer, DateTime, JSON, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Office(Base):
    __tablename__ = "offices"
    __table_args__ = (
        UniqueConstraint("office_key", name="uq_offices_office_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    office_key: Mapped[str] = mapped_column(String(50), nullable=False)
    holder_slug: Mapped[str | None] = mapped_column(String(100), nullable=True)
    institution: Mapped[str] = mapped_column(String(50), nullable=False)
    perms_json: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=dict)
    fill_strategy: Mapped[str] = mapped_column(String(20), nullable=False)
    term_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    term_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
