"""S1-5 镇财政 — the town's public account (tax in, wages / public spending out).

Deliberately a *thin* wrapper over the atomic write idioms ``coin_service``
already proves against ``resident_treasuries``: same guarded UPDATE / dialect
upsert, same three hard API rules. Only the table and the PK column differ.

Three hard rules copied verbatim from ``coin_service`` (violating any of them
is a bug, so they are restated here):

1. ``amount <= 0`` is a silent no-op (``tax``) / ``False`` (``disburse``) — never
   an exception, never a row write.
2. When a guard matches **0 rows, never call ``db.rollback()``**. Nothing was
   written, so there is nothing to undo, and rollback expires EVERY ORM object
   in the caller's session → ``MissingGreenlet`` on the next lazy attribute
   access under asyncio (see ``coin_service.charge``'s comment). Rollback only
   after a real write (the IntegrityError upsert retry below).
3. ``synchronize_session=False`` leaves already-loaded ORM rows stale — callers
   must re-SELECT (``balance()``) rather than read a cached object. The funded
   wage path additionally refreshes ``duty_service.set_wallet_cache``.

Auditability: town flows are NOT ledger rows (``transactions.user_id`` is a
users.id FK — see ``app/models/town_treasury.py``); ``balance_sc`` +
``updated_at`` are the audit surface, and the nightly job stamps
``town_last_spend_at`` through ``ConfigService``.

INTERFACE FREEZE (S1-5 §8 downstream contract). ``tax`` / ``disburse`` /
``balance`` are consumed by S2-5 (税率进政策表), S2-2 (镇长财政排序权), S5-8
(医疗补贴) and S5-9 (遗产充公). Their signatures are frozen:

    async def tax(db, amount: int, reason: str = "") -> None
    async def disburse(db, amount: int, reason: str = "") -> bool
    async def balance(db) -> int
"""
from __future__ import annotations

import logging
from datetime import datetime, UTC

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_config import SystemConfig
from app.models.town_treasury import TOWN_KEY, TownTreasury

logger = logging.getLogger(__name__)

# ConfigService key stamped by the nightly public-spending job (§2 任务 5): the
# scalar policy state lives in system_config, not in new columns.
LAST_SPEND_KEY = "town_last_spend_at"

# M-A C5: the fractional tax ledger. Same "scalar state lives in system_config"
# discipline as LAST_SPEND_KEY — the sub-1-SC remainder of every skim accrues
# here instead of evaporating in an ``int()``, so no migration is needed.
TAX_CARRY_KEY = "town_tax_carry"


async def balance(db: AsyncSession) -> int:
    """The town's current balance; 0 when the account row does not exist yet
    (mirrors ``coin_service.treasury_balance``)."""
    row = await db.execute(
        select(TownTreasury.balance_sc).where(TownTreasury.key == TOWN_KEY)
    )
    return row.scalar_one_or_none() or 0


async def tax_pending(db: AsyncSession, amount: int, reason: str = "") -> None:
    """Flush-owned town credit (upsert). The caller owns the transaction.

    Mirrors ``coin_service.treasury_credit_pending``: dialect-native
    ``ON CONFLICT DO UPDATE`` on postgres/sqlite, and on any other dialect the
    guarded UPDATE → insert-when-zero-rows fallback.
    """
    if amount <= 0:
        return
    now = datetime.now(UTC)
    values = {"key": TOWN_KEY, "balance_sc": amount, "updated_at": now}
    dialect = db.get_bind().dialect.name
    if dialect in ("postgresql", "sqlite"):
        insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
        statement = insert(TownTreasury).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[TownTreasury.key],
            set_={
                "balance_sc": TownTreasury.balance_sc + amount,
                "updated_at": now,
            },
        )
        await db.execute(statement)
    else:
        result = await db.execute(
            update(TownTreasury)
            .where(TownTreasury.key == TOWN_KEY)
            .values(balance_sc=TownTreasury.balance_sc + amount, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:
            db.add(TownTreasury(**values))
    await db.flush()


async def tax(db: AsyncSession, amount: int, reason: str = "") -> None:
    """Credit the town treasury (sales tax / gift tax / fines / escheat).

    ``amount <= 0`` is a silent no-op. The town row is created on demand.
    ``reason`` is accepted for call-site readability and symmetry with
    ``coin_service`` but is not persisted — there is no town ledger table (see
    the module docstring).
    """
    if amount <= 0:
        return
    await tax_pending(db, amount, reason)
    await db.commit()


async def kv_read(db: AsyncSession, key: str, default: str | None = None) -> str | None:
    """Read a raw ``system_config`` value (no JSON decoding, no session churn).

    Deliberately not ``ConfigService.get``: this runs inside transactions that
    the caller owns, and the value read here is written back by
    ``kv_upsert_pending`` — both halves stay on the raw string so a decode/encode
    round trip can never drift the ledger.
    """
    row = await db.execute(
        select(SystemConfig.value).where(SystemConfig.key == key)
    )
    value = row.scalar_one_or_none()
    return default if value is None else value


async def kv_upsert_pending(
    db: AsyncSession,
    key: str,
    value: str,
    *,
    group: str = "town",
    updated_by: str,
) -> None:
    """Flush-owned ``system_config`` upsert. The caller owns the transaction.

    Mirrors ``tax_pending`` exactly (dialect-native ``ON CONFLICT DO UPDATE`` on
    postgres/sqlite, guarded UPDATE → insert-when-zero-rows elsewhere), and NOT
    ``ConfigService.set`` — that one commits internally, which would tear a
    single-transaction settlement in half.

    ``group`` and ``updated_by`` are mandatory columns on ``SystemConfig`` (no
    server default): omitting either is a NOT NULL crash under create_all.
    """
    now = datetime.now(UTC)
    values = {
        "key": key, "value": value, "group": group,
        "updated_at": now, "updated_by": updated_by,
    }
    dialect = db.get_bind().dialect.name
    if dialect in ("postgresql", "sqlite"):
        insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
        statement = insert(SystemConfig).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[SystemConfig.key],
            set_={"value": value, "updated_at": now, "updated_by": updated_by},
        )
        await db.execute(statement)
    else:
        result = await db.execute(
            update(SystemConfig)
            .where(SystemConfig.key == key)
            .values(value=value, updated_at=now, updated_by=updated_by)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:
            db.add(SystemConfig(**values))
    await db.flush()


async def _skim(
    db: AsyncSession, gross: int, fallback_rate: float, reason: str,
) -> tuple[int, bool]:
    """M-A C5 core: return ``(cut, wrote_anything)``.

    The write flag is what lets ``skim_tax`` commit only when there is something
    to commit — the legacy path never committed on a zero cut, and a gratuitous
    commit would land whatever else the caller had pending.
    """
    from app.config import settings
    if not settings.town_treasury_enabled:
        return 0, False

    # S2-5: once policy storage is enabled the typed tax_rate is the single town
    # ratio; gate off keeps the caller's legacy per-channel rate.
    from app.services import fiscal_policy_service
    rate = await fiscal_policy_service.tax_rate(db, fallback=fallback_rate)
    exact = gross * rate

    if not settings.tax_carry_enabled:
        # Byte-level status quo: plain ``int()`` truncation, carry row untouched.
        # vm212 already runs with TOWN_TREASURY_ENABLED on, so this branch is the
        # one a dark deploy must not move by a single SC.
        cut = int(exact)
        if cut <= 0:
            return 0, False
        cut = min(cut, gross)
        await tax_pending(db, cut, reason)
        return cut, True

    carry = float(await kv_read(db, TAX_CARRY_KEY, "0") or "0")
    total = exact + carry
    cut = min(int(total), gross)
    if cut <= 0 and exact <= 0:
        # Nothing accrued (zero rate / zero gross): leave the ledger alone so a
        # no-op skim stays a no-op write.
        return 0, False
    await kv_upsert_pending(
        db, TAX_CARRY_KEY, f"{total - cut:.6f}", updated_by=f"skim_tax:{reason}",
    )
    if cut > 0:
        await tax_pending(db, cut, reason)
    return cut, True


async def skim_tax_pending(
    db: AsyncSession, gross: int, fallback_rate: float, reason: str = "",
) -> int:
    """Flush-owned tax skim for single-transaction NPC paths (M-A C2/C4).

    Returns the SC actually levied. ``town_treasury_enabled`` off → 0 and zero
    writes. With ``tax_carry_enabled`` on, the sub-SC remainder accrues into
    ``town_tax_carry`` and is levied once it crosses 1 SC; with it off the result
    is byte-identical to the legacy ``int(gross * rate)`` truncation.
    """
    cut, _ = await _skim(db, gross, fallback_rate, reason)
    return cut


async def skim_tax(
    db: AsyncSession, gross: int, fallback_rate: float, reason: str = "",
) -> int:
    """Self-committing tax skim for the player paths (``shop_effects``).

    The legacy implementation went through ``tax`` (self-committing), and the tip
    path has no guaranteed commit after the sentinel-creator branch — a pending
    version would silently drop the levy. So this version owns the commit, and
    only when something was actually written (see ``_skim``).
    """
    cut, wrote = await _skim(db, gross, fallback_rate, reason)
    if wrote:
        await db.commit()
    return cut


async def disburse(db: AsyncSession, amount: int, reason: str = "") -> bool:
    """Spend from the town treasury (wage funding / public works / subsidies).

    Atomic ``UPDATE ... WHERE key = 'town' AND balance_sc >= amount`` — returns
    False on insufficient funds (or a missing account) rather than raising, and
    NEVER rolls back on the zero-row path (rule 2 above).
    """
    if amount <= 0:
        return False
    result = await db.execute(
        update(TownTreasury)
        .where(TownTreasury.key == TOWN_KEY, TownTreasury.balance_sc >= amount)
        .values(
            balance_sc=TownTreasury.balance_sc - amount,
            updated_at=datetime.now(UTC),
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return (result.rowcount or 0) > 0


async def notify_changed(db: AsyncSession, *, delta: int, reason: str = "") -> bool:
    """Broadcast a ``treasury_changed`` WS envelope for a significant balance
    move. Returns whether anything was sent.

    Anchored to the world revision / seq the art spec froze for
    ``world_changed`` v1 (``seq`` reuses the OutboxEvent cursor — no new
    counter), same shape S2-1's ``office_changed`` uses.

    Rate discipline: high-frequency micro-skims must never spam the channel, so
    a broadcast needs ``|delta| >= town_ws_min_delta_sc`` and the default of 0
    disables broadcasting entirely — the nightly job is the intended trigger.
    Gated + fail-open: a broadcast failure never breaks a write.
    """
    try:
        from app.config import settings
        if not settings.town_treasury_enabled:
            return False
        threshold = int(settings.town_ws_min_delta_sc or 0)
        if threshold <= 0 or abs(int(delta)) < threshold:
            return False
        import uuid
        from app.services import world_revision_service as wrsvc
        payload = {
            "type": "treasury_changed",
            "schema_version": wrsvc.SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "seq": await wrsvc.current_source_cursor(db),
            "world_revision_id": await wrsvc.current_revision_id(db),
            "delta_sc": int(delta),
            "balance_sc": await balance(db),
            "reason": reason,
            "occurred_at": datetime.now(UTC).isoformat(),
        }
        from app.lab import apply as apply_engine
        await apply_engine.broadcast_world_changed(payload)
        return True
    except Exception:
        logger.warning("treasury_changed broadcast failed (%s)", reason, exc_info=True)
        return False


async def run_public_spending(db: AsyncSession) -> int:
    """Nightly public spending / reconciliation (S1-5 §2 任务 5). Returns the SC
    actually disbursed.

    Wages already leave the treasury per-resident through ``duty_service
    ._pay_wage``, so this job owns only the *periodic* outlay: a public-works
    budget (``town_public_works_daily_sc``, default 0 = reconcile-only) clamped
    to the current balance — the town never deficit-spends — plus a
    ``town_last_spend_at`` stamp in ``system_config`` so the burn-in probe can
    tell "ran and had nothing to spend" from "never ran".

    Gate off → whole no-op, including the stamp (the cron also guards, so a
    disabled world touches no DB at all). Real-time cadence by design: this is a
    daily operations job, not a world-clock rhythm.
    """
    from app.config import settings
    if not settings.town_treasury_enabled:
        return 0

    spent = 0
    budget = int(settings.town_public_works_daily_sc or 0)
    if budget > 0:
        amount = min(budget, await balance(db))
        if amount > 0 and await disburse(db, amount, reason="public_works"):
            spent = amount
            # The nightly job is the intended low-frequency WS trigger (§2 任务 6).
            await notify_changed(db, delta=-spent, reason="public_works")

    from app.services.config_service import ConfigService
    await ConfigService(db).set(
        LAST_SPEND_KEY, datetime.now(UTC).isoformat(),
        group="town", updated_by="nightly_public_spending",
    )
    return spent
