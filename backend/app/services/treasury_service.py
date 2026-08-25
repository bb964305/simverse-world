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
4. 锁序军规(F8):同一个事务要触碰 ``town_treasuries`` 与
   ``resident_treasuries`` 两张表时,**必须先动 town 行、再动 resident 行**
   (``town_to_resident`` 的 FOR UPDATE → credit 即该序;``npc_trade_service
   ._buy`` 的 skim → debit/credit 同序)。倒过来写就是与工资路径的 AB-BA
   环——真 PG 下 DeadlockDetected,被 kill 一侧的 rollback 还会 expire 调用方
   session 的全部 ORM 对象。

Auditability: town flows cannot use ``transactions`` (its ``user_id`` is a
hard users FK), so they are mirrored into the dedicated append-only
``town_treasury_entries`` ledger in the *same transaction* as the balance
movement — but only behind ``town_ledger_enabled`` (default off: the 058
migration and the mirror-write behaviour never ship in the same change).

INTERFACE FREEZE (S1-5 §8 downstream contract). ``tax`` / ``disburse`` /
``balance`` are consumed by S2-5 (税率进政策表), S2-2 (镇长财政排序权), S5-8
(医疗补贴) and S5-9 (遗产充公). Their signatures are frozen:

    async def tax(db, amount: int, reason: str = "") -> None
    async def disburse(db, amount: int, reason: str = "") -> bool
    async def balance(db) -> int
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, UTC
import uuid

from sqlalchemy import BigInteger, String, cast, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.system_config import SystemConfig
from app.models.town_treasury import TOWN_KEY, TownTreasury
from app.models.town_treasury_entry import TownTreasuryEntry

logger = logging.getLogger(__name__)

# ConfigService key stamped by the nightly public-spending job (§2 任务 5): the
# scalar policy state lives in system_config, not in new columns.
LAST_SPEND_KEY = "town_last_spend_at"

# M-A C5: the fractional tax ledger. Same "scalar state lives in system_config"
# discipline as LAST_SPEND_KEY — the sub-1-SC remainder of every skim accrues
# here instead of evaporating in an ``int()``, so no migration is needed.
#
# M-A 加固:值是**整数 milli-SC**(1 SC = 1000),不再是 "0.800000" 这样的浮点
# 串——只有整数才能走 ``kv_add_int_pending`` 的数据库内原子增量。键名跟着换
# (``town_tax_carry`` → ``town_tax_carry_milli``):单位藏在值里迟早出事,而且
# 万一哪个 dev 库里还留着老键的浮点串,新版的 CAST 在真 PostgreSQL 上会直接抛
# (sqlite 则静默截成 0)。老键从此无人读写,留着不动(删数据不进这次变更)。
TAX_CARRY_KEY = "town_tax_carry_milli"
CARRY_SCALE = 1000                              # 1 SC = 1000 milli


def _entry_ref(ref_key: str | None = None) -> str:
    value = ref_key or f"town:{uuid.uuid4()}"
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValueError("town ledger ref_key must be 1..200 characters")
    return value


async def _append_entry_pending(
    db: AsyncSession,
    amount: int,
    balance_after: int,
    reason: str,
    *,
    resident_slug: str | None = None,
    ref_key: str | None = None,
) -> None:
    """Append one signed audit row; the caller owns commit/rollback.

    Gated behind ``town_ledger_enabled`` (default off): the 058 migration and
    the mirror-write behaviour must not ship in the same change. Gate off →
    zero ledger writes, money paths untouched; re-anchor before opening it.
    """
    from app.config import settings
    if not settings.town_ledger_enabled:
        return
    if amount == 0:
        return
    db.add(TownTreasuryEntry(
        town_key=TOWN_KEY,
        amount_sc=int(amount),
        balance_after_sc=int(balance_after),
        reason=(reason or "unspecified")[:200],
        resident_slug=resident_slug,
        ref_key=_entry_ref(ref_key),
    ))
    await db.flush()


async def balance(db: AsyncSession) -> int:
    """The town's current balance; 0 when the account row does not exist yet
    (mirrors ``coin_service.treasury_balance``)."""
    row = await db.execute(
        select(TownTreasury.balance_sc).where(TownTreasury.key == TOWN_KEY)
    )
    return row.scalar_one_or_none() or 0


async def tax_pending(
    db: AsyncSession,
    amount: int,
    reason: str = "",
    *,
    ref_key: str | None = None,
) -> None:
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
        ).returning(TownTreasury.balance_sc)
        balance_after = (await db.execute(statement)).scalar_one()
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
        balance_after = (await db.execute(
            select(TownTreasury.balance_sc).where(TownTreasury.key == TOWN_KEY)
        )).scalar_one()
    await db.flush()
    await _append_entry_pending(
        db, amount, balance_after, reason, ref_key=ref_key,
    )


async def tax(db: AsyncSession, amount: int, reason: str = "") -> None:
    """Credit the town treasury (sales tax / gift tax / fines / escheat).

    ``amount <= 0`` is a silent no-op. The town row is created on demand and
    ``reason`` is persisted in the dedicated town ledger in the same transaction.
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


async def kv_read_int(db: AsyncSession, key: str) -> int:
    """读一个整数 KV(缺行 / 值不是整数 → 0)。

    绝不抛:这些键是**记账**不是钱,一个脏值不该把调用它的那笔买卖连坐掉。
    """
    raw = await kv_read(db, key)
    try:
        return int(raw)                     # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


async def kv_add_int_pending(
    db: AsyncSession,
    key: str,
    delta: int,
    *,
    group: str = "town",
    updated_by: str,
) -> None:
    """原子增量 upsert:新值由**数据库**在写的那一刻从当前值算出来。

    与 ``kv_upsert_pending`` 只差这一点,但那正是竞态的根:盲写版本写回的是调
    用方几毫秒前读到的值,两个进程同时累尾数时后写的抹掉先写的
    (last-writer-wins)。这里 ``value = CAST(value AS BIGINT) + delta`` 整条在
    SQL 里,方言分派与 ``tax_pending`` 逐行同构。

    值必须是纯整数串(milli-SC),不能是 ``"0.8"``:真 PostgreSQL 上
    ``CAST('0.8' AS BIGINT)`` 直接抛,sqlite 则静默截成 0。
    """
    now = datetime.now(UTC)
    delta = int(delta)
    values = {
        "key": key, "value": str(delta), "group": group,
        "updated_at": now, "updated_by": updated_by,
    }
    bumped = cast(cast(SystemConfig.value, BigInteger) + delta, String)
    dialect = db.get_bind().dialect.name
    if dialect in ("postgresql", "sqlite"):
        insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
        statement = insert(SystemConfig).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[SystemConfig.key],
            set_={"value": bumped, "updated_at": now, "updated_by": updated_by},
        )
        await db.execute(statement)
    else:
        result = await db.execute(
            update(SystemConfig)
            .where(SystemConfig.key == key)
            .values(value=bumped, updated_at=now, updated_by=updated_by)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:
            db.add(SystemConfig(**values))
    await db.flush()


async def kv_take_int_pending(
    db: AsyncSession, key: str, amount: int, *, updated_by: str,
) -> bool:
    """守卫扣减:``... SET value = value - amount WHERE value >= amount``。

    返回是否真扣到。零行 = 别人抢先扣走了,**不 rollback**(军规 2:什么都没写
    就没有什么要撤,而 rollback 会 expire 调用方 session 里的所有 ORM 对象)。
    """
    if amount <= 0:
        return False
    amount = int(amount)
    result = await db.execute(
        update(SystemConfig)
        .where(SystemConfig.key == key,
               cast(SystemConfig.value, BigInteger) >= amount)
        .values(
            value=cast(cast(SystemConfig.value, BigInteger) - amount, String),
            updated_at=datetime.now(UTC),
            updated_by=updated_by,
        )
        .execution_options(synchronize_session=False)
    )
    await db.flush()
    return (result.rowcount or 0) > 0


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

    # M-A 加固:两步都在数据库里做完,Python 侧不留读-改-写窗口。
    # ① 尾数原子累加(谁也抹不掉谁);② 凑满整 SC 走守卫兑换(零行 = 别的进程
    # 抢先兑走了,这一笔就只累不征——钱一分不少地留在账上,下一笔再兑)。
    # 单线程下与旧算法逐笔等价:旧 `total = exact + carry; cut = min(int(total),
    # gross); carry' = total - cut`,新 `carry' = carry + exact_milli;
    # cut = min(carry'//1000, gross); carry'' = carry' - cut*1000` —— 同一个式子
    # 换成整数(milli 粒度的四舍五入是唯一差别,见 CARRY_SCALE)。
    exact_milli = int(round(exact * CARRY_SCALE))
    wrote = False
    if exact_milli > 0:
        await kv_add_int_pending(
            db, TAX_CARRY_KEY, exact_milli, updated_by=f"skim_tax:{reason}")
        wrote = True

    carry_milli = await kv_read_int(db, TAX_CARRY_KEY)   # 含自己刚 flush 的那笔
    cut = min(carry_milli // CARRY_SCALE, gross)
    if cut > 0 and await kv_take_int_pending(
            db, TAX_CARRY_KEY, cut * CARRY_SCALE, updated_by=f"skim_tax:{reason}"):
        await tax_pending(db, cut, reason)
        wrote = True
    else:
        cut = 0
    if not wrote:
        # Nothing accrued (zero rate / zero gross): leave the ledger alone so a
        # no-op skim stays a no-op write.
        return 0, False
    return cut, True


async def skim_tax_pending(
    db: AsyncSession, gross: int, fallback_rate: float, reason: str = "",
) -> int:
    """Flush-owned tax skim for single-transaction NPC paths (M-A C2/C4).

    Returns the SC actually levied. ``town_treasury_enabled`` off → 0 and zero
    writes. With ``tax_carry_enabled`` on, the sub-SC remainder accrues into
    ``town_tax_carry_milli``(整数 milli-SC,原子增量)and is levied once it
    crosses 1 SC; with it off the result is byte-identical to the legacy
    ``int(gross * rate)`` truncation.
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
        .returning(TownTreasury.balance_sc)
        .execution_options(synchronize_session=False)
    )
    balance_after = result.scalar_one_or_none()
    landed = balance_after is not None
    if landed:
        await _append_entry_pending(db, -amount, balance_after, reason)
    # Keep the historical transaction-owner contract even on a zero-row guard.
    # In-memory SQLite tests share one connection across sessions; letting a
    # losing session close with an open transaction would roll back a winner.
    await db.commit()
    return landed


async def wage_window_totals(
    db: AsyncSession, *, window_days: int = 7,
) -> tuple[int, int]:
    """Return ``(realized_income, public_wages)`` for the trailing window.

    The migration's opening anchor is deliberately excluded from income: it
    explains pre-ledger stock, but is not recurring revenue that can sustain a
    payroll.  Explicit future grants remain ordinary positive income.
    """
    since = datetime.now(UTC) - timedelta(days=max(1, int(window_days)))
    income = (await db.execute(
        select(func.coalesce(func.sum(TownTreasuryEntry.amount_sc), 0)).where(
            TownTreasuryEntry.town_key == TOWN_KEY,
            TownTreasuryEntry.created_at >= since,
            TownTreasuryEntry.amount_sc > 0,
            TownTreasuryEntry.reason != "opening_balance",
        )
    )).scalar_one()
    wages = (await db.execute(
        select(func.coalesce(func.sum(-TownTreasuryEntry.amount_sc), 0)).where(
            TownTreasuryEntry.town_key == TOWN_KEY,
            TownTreasuryEntry.created_at >= since,
            TownTreasuryEntry.amount_sc < 0,
            TownTreasuryEntry.reason.like("wage:%"),
        )
    )).scalar_one()
    return int(income or 0), int(wages or 0)


async def town_to_resident(
    db: AsyncSession,
    resident_slug: str,
    amount: int,
    *,
    reason: str,
    ref_key: str | None = None,
    wage_budget_ratio: float | None = None,
    wage_window_days: int = 7,
) -> bool:
    """Atomically move town funds into one resident treasury.

    The town row is locked before the optional rolling-income budget is read,
    serialising competing wage payments on PostgreSQL.  Debit, resident
    credit, and the signed ledger row share one commit; any failure rolls the
    whole move back.  A stable ``ref_key`` makes a retry a no-op.
    """
    if amount <= 0:
        return False
    if ref_key is not None:
        ref_key = _entry_ref(ref_key)
    try:
        locked_balance = (await db.execute(
            select(TownTreasury.balance_sc)
            .where(TownTreasury.key == TOWN_KEY)
            .with_for_update()
        )).scalar_one_or_none()
        if locked_balance is None or locked_balance < amount:
            # This function owns the transaction.  Even a SELECT FOR UPDATE
            # no-op must end it so the town row is never left locked in a
            # long-lived caller session.
            await db.commit()
            return False

        if ref_key and (await db.execute(
            select(TownTreasuryEntry.id).where(
                TownTreasuryEntry.ref_key == ref_key
            ).limit(1)
        )).scalar_one_or_none() is not None:
            await db.commit()
            return False

        if wage_budget_ratio is not None:
            ratio = min(1.0, max(0.0, float(wage_budget_ratio)))
            income, wages = await wage_window_totals(
                db, window_days=wage_window_days,
            )
            income_budget = int(income * ratio)
            from app.config import settings
            reserve_floor = max(0, int(getattr(settings, "town_wage_reserve_floor_sc", 20)))
            reserve_available = max(0, int(locked_balance - reserve_floor))

            # The wage can be funded if either:
            # 1. Rolling income wage budget has room: wages + amount <= income_budget
            # 2. Or town treasury has surplus reserve above reserve_floor: amount <= reserve_available
            if wages + amount > income_budget and amount > reserve_available:
                logger.info(
                    "town wage budget exhausted: resident=%s amount=%d "
                    "income=%d wages=%d ratio=%.3f balance=%d floor=%d",
                    resident_slug, amount, income, wages, ratio, locked_balance, reserve_floor,
                )
                await db.commit()
                return False

        debited = await db.execute(
            update(TownTreasury)
            .where(
                TownTreasury.key == TOWN_KEY,
                TownTreasury.balance_sc >= amount,
            )
            .values(
                balance_sc=TownTreasury.balance_sc - amount,
                updated_at=datetime.now(UTC),
            )
            .returning(TownTreasury.balance_sc)
            .execution_options(synchronize_session=False)
        )
        balance_after = debited.scalar_one_or_none()
        if balance_after is None:
            await db.commit()
            return False

        from app.services import coin_service
        await coin_service.treasury_credit_pending(
            db, resident_slug, amount, reason=reason,
        )
        await _append_entry_pending(
            db, -amount, balance_after, reason,
            resident_slug=resident_slug, ref_key=ref_key,
        )
        await db.commit()
        return True
    except Exception:
        await db.rollback()
        raise


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
