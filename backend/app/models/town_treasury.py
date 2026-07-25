from datetime import datetime, UTC

from sqlalchemy import String, Integer, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

# Single-town MVP: one row, keyed 'town'. Multi-town would key by town slug.
TOWN_KEY = "town"


class TownTreasury(Base):
    """Town treasury — the third kind of account in this world (S1-5).

    Before S1-5 money lived in exactly two places: ``users.soul_coin_balance``
    (player wallets) and ``resident_treasuries.balance_sc`` (one NPC's purse).
    There was no town / collective / public account at all, so duty wages were
    minted out of nothing. This table is that missing public account: taxes flow
    in (``TreasuryService.tax``), wages and public spending flow out
    (``TreasuryService.disburse``).

    Shape is deliberately identical to ``ResidentTreasury`` (slug-ish PK +
    ``balance_sc`` + ``updated_at``) so the already-proven atomic write idioms in
    ``coin_service`` transfer verbatim.

    AUDIT NOTE (deliberate deviation, same as ``resident_treasury.py``): town
    flows are NOT mirrored into the ``transactions`` ledger — ``transactions
    .user_id`` is a hard FK to ``users.id``, so a synthetic town account cannot
    be a ledger row. Auditability rests on ``balance_sc`` + ``updated_at``.
    Scalar policy knobs (tax rate, last_collected_at) live in ``system_config``
    via ``ConfigService`` rather than in new columns here.
    """

    __tablename__ = "town_treasuries"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    balance_sc: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )
