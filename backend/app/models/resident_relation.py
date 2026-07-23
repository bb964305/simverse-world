import uuid
from datetime import datetime, UTC

from sqlalchemy import String, Integer, Float, DateTime, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ResidentRelation(Base):
    """Numeric two-axis relationship strength between two parties (P2 §7.1).

    Unifies resident-resident and resident-player ties in one row, keyed by a
    *canonical undirected pair*: ``party_a``/``party_b`` are the two party ids
    sorted so the lexicographically-smaller id is always ``party_a`` (see
    ``relation_service.canonical_pair``). That normalisation + the unique index
    below guarantees a single row per unordered pair (no ``(x,y)`` **and**
    ``(y,x)`` duplicates).

    Two independent axes, both rule-driven (zero LLM):
    - ``familiarity`` [0, 1] — quantity of contact (meetings, witnessing).
    - ``affinity``   [-1, 1] — quality of contact (positive/negative outcomes).

    The existing natural-language relationship *memory* is kept untouched: text
    carries the qualitative description, these numbers carry the drivers.
    """

    __tablename__ = "resident_relations"
    __table_args__ = (
        UniqueConstraint("party_a", "party_b", name="uq_resident_relation_pair"),
        # party_a is already the leading column of the unique index (forward
        # lookups); this covers "all relations of X" when X lands in party_b.
        Index("ix_resident_relation_party_b", "party_b"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    party_a: Mapped[str] = mapped_column(String)
    party_a_type: Mapped[str] = mapped_column(String(20), default="resident")
    party_b: Mapped[str] = mapped_column(String)
    party_b_type: Mapped[str] = mapped_column(String(20), default="resident")
    familiarity: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    affinity: Mapped[float] = mapped_column(Float, default=0.0, server_default="0")
    interact_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_interact_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
