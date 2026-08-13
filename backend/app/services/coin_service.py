from datetime import UTC, datetime
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.coin_hold_entry import CoinHoldEntry
from app.models.user import User
from app.models.transaction import Transaction
from app.models.coin_hold import CoinHold
from app.models.resident_treasury import ResidentTreasury
from app.services.system_users import NON_USER_CREATOR_IDS

MAX_TRANSFER_ATTEMPTS = 3


class CoinError(Exception):
    """Raised on invalid hold/settle/refund operations (router maps to 4xx)."""


async def get_balance(db: AsyncSession, user_id: str) -> int:
    result = await db.execute(select(User.soul_coin_balance).where(User.id == user_id))
    balance = result.scalar_one_or_none()
    return balance or 0


async def charge(db: AsyncSession, user_id: str, amount: int, reason: str) -> bool:
    """Debit ``amount`` from a user. Atomic ``UPDATE ... WHERE balance >= amount``
    so concurrent charges can't oversell (the old SELECT-then-decrement raced).

    NOTE: uses ``synchronize_session=False`` — any ORM ``User`` already loaded in
    this session is left stale; callers read the fresh balance via a new SELECT
    (``get_balance``) or a WS coin_update, never off the cached object.
    """
    if amount <= 0:
        return False
    result = await db.execute(
        update(User)
        .where(User.id == user_id, User.soul_coin_balance >= amount)
        .values(soul_coin_balance=User.soul_coin_balance - amount)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        # Guard matched 0 rows → nothing was written, so there is nothing to undo.
        # Do NOT rollback here: rollback expires EVERY ORM object in the caller's
        # session (this is independent of expire_on_commit=False, which only
        # governs commit), so a later lazy attribute access — e.g. resident.id in
        # the ws chat wake path — raises MissingGreenlet under asyncio. Leave the
        # (empty) transaction for the caller to manage.
        return False
    db.add(Transaction(user_id=user_id, amount=-amount, reason=reason))
    await db.commit()
    return True


async def charge_pending(
    db: AsyncSession, user_id: str, amount: int, reason: str
) -> bool:
    """Flush-only debit primitive. It never commits or rolls back."""
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        return False
    result = await db.execute(
        update(User)
        .where(User.id == user_id, User.soul_coin_balance >= amount)
        .values(soul_coin_balance=User.soul_coin_balance - amount)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        return False
    db.add(Transaction(user_id=user_id, amount=-amount, reason=reason))
    await db.flush()
    return True


def _sqlstate(exc: BaseException) -> str | None:
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        for name in ("sqlstate", "pgcode"):
            value = getattr(current, name, None)
            if isinstance(value, str):
                return value
        for name in ("orig", "__cause__", "__context__"):
            nested = getattr(current, name, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    return None


def is_retryable_transaction_error(exc: BaseException) -> bool:
    return _sqlstate(exc) in {"40001", "40P01"}


async def lock_user_accounts(
    db: AsyncSession, user_ids: Sequence[str]
) -> list[str]:
    ordered_ids = sorted({user_id for user_id in user_ids if user_id})
    if not ordered_ids:
        return []
    found = set(
        (
            await db.execute(
                select(User.id)
                .where(User.id.in_(ordered_ids))
                .order_by(User.id)
                .with_for_update()
            )
        ).scalars()
    )
    return sorted(set(ordered_ids) - found)


async def transfer(db: AsyncSession, from_user: str, to_user: str, amount: int, reason: str) -> bool:
    """Atomic P2P with the same global user-lock order as terminal settlement."""
    if amount <= 0 or from_user == to_user:
        return False
    for attempt in range(1, MAX_TRANSFER_ATTEMPTS + 1):
        savepoint = await db.begin_nested()
        try:
            missing = await lock_user_accounts(db, (from_user, to_user))
            if missing:
                await savepoint.rollback()
                return False
            debited = await db.execute(
                update(User)
                .where(User.id == from_user, User.soul_coin_balance >= amount)
                .values(soul_coin_balance=User.soul_coin_balance - amount)
                .execution_options(synchronize_session=False)
            )
            if debited.rowcount == 0:
                # The savepoint releases the ordered row locks without expiring
                # the caller's whole session.
                await savepoint.rollback()
                return False
            credited = await db.execute(
                update(User)
                .where(User.id == to_user)
                .values(soul_coin_balance=User.soul_coin_balance + amount)
                .execution_options(synchronize_session=False)
            )
            if credited.rowcount == 0:
                await savepoint.rollback()
                return False
            db.add(Transaction(user_id=from_user, amount=-amount, reason=reason))
            db.add(Transaction(user_id=to_user, amount=amount, reason=reason))
            await savepoint.commit()
            await db.commit()
            return True
        except Exception as exc:
            if savepoint.is_active:
                await savepoint.rollback()
            retryable = is_retryable_transaction_error(exc)
            if not retryable or attempt == MAX_TRANSFER_ATTEMPTS:
                raise
            await db.rollback()
    raise AssertionError("transfer retry loop exhausted")


async def hold_pending(
    db: AsyncSession,
    user_id: str,
    amount: int,
    reason: str,
    *,
    terminalization_version: str = "v1",
) -> CoinHold | None:
    """Flush-only variant of :func:`hold`: the debit + CoinHold + ledger row are
    flushed, NOT committed, so the caller can commit them in ONE transaction with
    related state (e.g. a LabTask + its ``hold_id`` linkage) — closing the crash
    window where a task committed without its hold, or a hold without its task
    link (recovery plan Phase 2, gap #9). Returns the (uncommitted) CoinHold with
    its ``id`` populated, or None if the balance guard matched 0 rows
    (insufficient funds) — in which case nothing was written for the hold and the
    caller owns the rollback."""
    if amount <= 0:
        return None
    if terminalization_version not in {"v1", "v2"}:
        raise CoinError("terminalization_version must be v1 or v2")
    debited = await db.execute(
        update(User)
        .where(User.id == user_id, User.soul_coin_balance >= amount)
        .values(soul_coin_balance=User.soul_coin_balance - amount)
        .execution_options(synchronize_session=False)
    )
    if debited.rowcount == 0:
        # Nothing written → no rollback here (would expire the caller's ORM
        # objects mid-flow); the caller decides whether to abandon. See charge().
        return None
    h = CoinHold(
        user_id=user_id,
        amount=amount,
        reason=reason,
        status="held",
        terminalization_version=terminalization_version,
        cutover_at=datetime.now(UTC) if terminalization_version == "v2" else None,
    )
    db.add(h)
    db.add(Transaction(user_id=user_id, amount=-amount, reason=f"hold:{reason}"))
    await db.flush()  # populate h.id without committing; caller owns the commit
    return h


async def hold(
    db: AsyncSession,
    user_id: str,
    amount: int,
    reason: str,
    *,
    terminalization_version: str = "v1",
) -> str | None:
    """Freeze ``amount`` from a user into a CoinHold (escrow). Debit + hold-row +
    ledger commit atomically. Returns hold_id, or None if insufficient funds."""
    h = await hold_pending(
        db,
        user_id,
        amount,
        reason,
        terminalization_version=terminalization_version,
    )
    if h is None:
        return None
    await db.commit()
    await db.refresh(h)
    return h.id


Split = tuple[str, int, str]


def validate_distribution(splits: Sequence[Split], hold_amount: int) -> list[Split]:
    """Validate the entire distribution without issuing a mutating statement."""
    if not isinstance(splits, (list, tuple)) or not splits:
        raise CoinError("settle requires at least one split")

    validated: list[Split] = []
    recipients: set[str] = set()
    total = 0
    for raw in splits:
        if not isinstance(raw, (list, tuple)) or len(raw) != 3:
            raise CoinError("each split must contain recipient, amount, and reason")
        recipient, amount, reason = raw
        if (
            not isinstance(recipient, str)
            or not recipient
            or recipient != recipient.strip()
        ):
            raise CoinError("split recipient is invalid")
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise CoinError("split amount must be a positive integer")
        if not isinstance(reason, str) or not reason or len(reason) > 100:
            raise CoinError("split reason must be a nonempty string of at most 100 characters")
        if recipient.startswith("treasury:"):
            slug = recipient.removeprefix("treasury:")
            if not slug or len(slug) > 100:
                raise CoinError("treasury recipient is invalid")
        elif recipient != "sink" and len(recipient) > 160:
            raise CoinError("user recipient is invalid")
        if recipient in recipients:
            raise CoinError(f"duplicate split recipient {recipient}")
        recipients.add(recipient)
        total += amount
        validated.append((recipient, amount, reason))

    if total != hold_amount:
        raise CoinError(f"settle splits sum {total} != hold amount {hold_amount}")
    return validated


async def _lock_hold(db: AsyncSession, hold_id: str, *, action: str) -> CoinHold:
    hold = (
        await db.execute(
            select(CoinHold).where(CoinHold.id == hold_id).with_for_update()
        )
    ).scalar_one_or_none()
    if hold is None or hold.status != "held":
        raise CoinError(f"hold {hold_id} not {action}able (missing or not held)")
    if (
        isinstance(hold.amount, bool)
        or not isinstance(hold.amount, int)
        or hold.amount <= 0
    ):
        raise CoinError(f"hold {hold_id} has an invalid amount")
    return hold


async def lock_distribution_accounts(
    db: AsyncSession, splits: Sequence[Split]
) -> None:
    """Lock account rows in the global Users -> Treasuries sorted order."""
    user_ids = [
        recipient
        for recipient, _, _ in splits
        if recipient != "sink" and not recipient.startswith("treasury:")
    ]
    if user_ids:
        missing = await lock_user_accounts(db, user_ids)
        if missing:
            raise CoinError(f"split recipients do not exist: {', '.join(missing)}")

    treasury_slugs = sorted(
        recipient.removeprefix("treasury:")
        for recipient, _, _ in splits
        if recipient.startswith("treasury:")
    )
    if treasury_slugs:
        now = datetime.now(UTC)
        dialect = db.get_bind().dialect.name
        for slug in treasury_slugs:
            values = {
                "resident_slug": slug,
                "balance_sc": 0,
                "updated_at": now,
            }
            if dialect == "postgresql":
                statement = postgresql_insert(ResidentTreasury).values(**values)
                await db.execute(statement.on_conflict_do_nothing())
            elif dialect == "sqlite":
                statement = sqlite_insert(ResidentTreasury).values(**values)
                await db.execute(statement.on_conflict_do_nothing())
            elif await db.get(ResidentTreasury, slug) is None:
                db.add(ResidentTreasury(**values))
                await db.flush()
        await db.execute(
            select(ResidentTreasury.resident_slug)
            .where(ResidentTreasury.resident_slug.in_(treasury_slugs))
            .order_by(ResidentTreasury.resident_slug)
            .with_for_update()
        )


async def reward_pending(
    db: AsyncSession, user_id: str, amount: int, reason: str
) -> bool:
    """Flush-owned credit primitive. It never commits or rolls back."""
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise CoinError("reward amount must be a positive integer")
    result = await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(soul_coin_balance=User.soul_coin_balance + amount)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        return False
    db.add(Transaction(user_id=user_id, amount=amount, reason=reason))
    await db.flush()
    return True


async def treasury_credit_pending(
    db: AsyncSession, slug: str, amount: int, reason: str = ""
) -> None:
    """Flush-owned treasury upsert. The caller owns the surrounding transaction."""
    if not isinstance(slug, str) or not slug or len(slug) > 100:
        raise CoinError("treasury slug is invalid")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise CoinError("treasury amount must be a positive integer")

    values = {"resident_slug": slug, "balance_sc": amount}
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(ResidentTreasury).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[ResidentTreasury.resident_slug],
            set_={"balance_sc": ResidentTreasury.balance_sc + amount},
        )
        await db.execute(statement)
    elif dialect == "sqlite":
        statement = sqlite_insert(ResidentTreasury).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[ResidentTreasury.resident_slug],
            set_={"balance_sc": ResidentTreasury.balance_sc + amount},
        )
        await db.execute(statement)
    else:
        result = await db.execute(
            update(ResidentTreasury)
            .where(ResidentTreasury.resident_slug == slug)
            .values(balance_sc=ResidentTreasury.balance_sc + amount)
            .execution_options(synchronize_session=False)
        )
        if result.rowcount == 0:
            db.add(ResidentTreasury(**values))
    await db.flush()


async def settle_pending(
    db: AsyncSession,
    hold_id: str,
    splits: Sequence[Split],
    *,
    operation_key: str | None = None,
) -> CoinHold:
    """Lock, validate, and flush a settlement without ending the transaction."""
    hold = await _lock_hold(db, hold_id, action="settle")
    validated = validate_distribution(splits, hold.amount)
    await lock_distribution_accounts(db, validated)

    now = datetime.now(UTC)
    claimed = await db.execute(
        update(CoinHold)
        .where(CoinHold.id == hold_id, CoinHold.status == "held")
        .values(status="settled", settled_at=now)
    )
    if (claimed.rowcount or 0) != 1:
        raise CoinError(f"hold {hold_id} lost settlement ownership")

    prefix = operation_key or f"settle:{hold_id}"
    for recipient, amount, reason in validated:
        if recipient.startswith("treasury:"):
            await treasury_credit_pending(
                db, recipient.removeprefix("treasury:"), amount, reason
            )
        elif recipient != "sink":
            if not await reward_pending(db, recipient, amount, reason):
                raise CoinError(f"split recipient {recipient} disappeared")
        db.add(
            CoinHoldEntry(
                hold_id=hold_id,
                terminal_action="settle",
                recipient_key=recipient,
                amount=amount,
                operation_key=f"{prefix}:{recipient}",
                reason=reason,
            )
        )
    await db.flush()
    return hold


async def refund_pending(
    db: AsyncSession,
    hold_id: str,
    reason: str,
    *,
    operation_key: str | None = None,
    splits: Sequence[Split] | None = None,
) -> CoinHold:
    """Lock, validate, and flush a refund distribution without committing."""
    hold = await _lock_hold(db, hold_id, action="refund")
    if not isinstance(reason, str) or not reason or len(reason) > 100:
        raise CoinError("refund reason must be a nonempty string of at most 100 characters")
    validated = validate_distribution(
        splits or [(hold.user_id, hold.amount, reason)], hold.amount
    )
    await lock_distribution_accounts(db, validated)

    now = datetime.now(UTC)
    claimed = await db.execute(
        update(CoinHold)
        .where(CoinHold.id == hold_id, CoinHold.status == "held")
        .values(status="refunded", settled_at=now)
    )
    if (claimed.rowcount or 0) != 1:
        raise CoinError(f"hold {hold_id} lost refund ownership")
    prefix = operation_key or f"refund:{hold_id}"
    for recipient, amount, split_reason in validated:
        if recipient.startswith("treasury:"):
            await treasury_credit_pending(
                db, recipient.removeprefix("treasury:"), amount, split_reason
            )
        elif recipient != "sink":
            if not await reward_pending(db, recipient, amount, split_reason):
                raise CoinError(f"refund recipient {recipient} disappeared")
        db.add(
            CoinHoldEntry(
                hold_id=hold_id,
                terminal_action="refund",
                recipient_key=recipient,
                amount=amount,
                operation_key=f"{prefix}:{recipient}",
                reason=split_reason,
            )
        )
    await db.flush()
    return hold


async def settle(db: AsyncSession, hold_id: str, splits: Sequence[Split]) -> None:
    """Compatibility transaction owner for non-Lab callers."""
    await settle_pending(db, hold_id, splits)
    await db.commit()


async def refund(db: AsyncSession, hold_id: str, reason: str) -> None:
    """Compatibility transaction owner for non-Lab callers."""
    await refund_pending(db, hold_id, reason)
    await db.commit()


async def treasury_balance(db: AsyncSession, slug: str) -> int:
    row = await db.execute(
        select(ResidentTreasury.balance_sc).where(ResidentTreasury.resident_slug == slug)
    )
    return row.scalar_one_or_none() or 0


async def treasury_credit(db: AsyncSession, slug: str, amount: int, reason: str = "") -> None:
    """Atomically add to a researcher's treasury (upsert). Recorded here rather
    than in transactions — transactions.user_id is a users.id FK, so a synthetic
    ``treasury:<slug>`` account can't be a ledger row (spec §4.7 deviation)."""
    if amount <= 0:
        return
    await treasury_credit_pending(db, slug, amount, reason)
    await db.commit()


async def treasury_debit_pending(db: AsyncSession, slug: str, amount: int) -> bool:
    """Flush-owned guarded debit — the caller owns the surrounding transaction
    (mirror of ``treasury_credit_pending``). False when the row is missing or
    short; it never commits, so the debit only exists once the caller says so."""
    if not isinstance(slug, str) or not slug or len(slug) > 100:
        raise CoinError("treasury slug is invalid")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise CoinError("treasury amount must be a positive integer")

    result = await db.execute(
        update(ResidentTreasury)
        .where(ResidentTreasury.resident_slug == slug, ResidentTreasury.balance_sc >= amount)
        .values(balance_sc=ResidentTreasury.balance_sc - amount)
        .execution_options(synchronize_session=False)
    )
    return (result.rowcount or 0) > 0


async def treasury_debit_with_reserve_pending(
    db: AsyncSession,
    slug: str,
    amount: int,
    *,
    minimum_remaining: int,
) -> bool:
    """Guarded resident debit that atomically preserves a wallet floor.

    Like :func:`treasury_debit_pending`, this never commits or rolls back.  The
    extra predicate closes the race between an affordability SELECT and a
    concurrent spend, which is required for market visitors' poverty reserve.
    """
    if not isinstance(slug, str) or not slug or len(slug) > 100:
        raise CoinError("treasury slug is invalid")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        raise CoinError("treasury amount must be a positive integer")
    if (isinstance(minimum_remaining, bool)
            or not isinstance(minimum_remaining, int)
            or minimum_remaining < 0):
        raise CoinError("minimum remaining balance must be a nonnegative integer")
    result = await db.execute(
        update(ResidentTreasury)
        .where(
            ResidentTreasury.resident_slug == slug,
            ResidentTreasury.balance_sc >= amount + minimum_remaining,
        )
        .values(balance_sc=ResidentTreasury.balance_sc - amount)
        .execution_options(synchronize_session=False)
    )
    return (result.rowcount or 0) > 0


async def treasury_transfer(
    db: AsyncSession, from_slug: str, to_slug: str, amount: int, reason: str = ""
) -> bool:
    """M-A C0: atomic resident→resident move. Debit-first, then credit, then a
    single commit — nothing survives a mid-flight failure: the pending debit is
    rolled back on the spot, never left hanging to ride a later unrelated commit
    (that is how the old debit+credit pair — each with its own commit — burned
    coins when the second leg failed)."""
    if from_slug == to_slug:
        raise CoinError("treasury transfer to self")
    if not await treasury_debit_pending(db, from_slug, amount):
        return False
    try:
        await treasury_credit_pending(db, to_slug, amount, reason)
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    return True


async def treasury_debit(db: AsyncSession, slug: str, amount: int, reason: str = "") -> bool:
    """Atomically spend from a treasury: ``UPDATE ... WHERE balance_sc >= amount``.
    Returns False if the balance is insufficient (used for proposal fuel, P3)."""
    if amount <= 0:
        return False
    res = await db.execute(
        update(ResidentTreasury)
        .where(ResidentTreasury.resident_slug == slug, ResidentTreasury.balance_sc >= amount)
        .values(balance_sc=ResidentTreasury.balance_sc - amount)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return (res.rowcount or 0) > 0


async def reward(db: AsyncSession, user_id: str, amount: int, reason: str) -> int:
    """Credit ``amount`` to a user. Atomic ``UPDATE ... balance = balance + amount``
    (mirrors charge()). The old SELECT-then-``user.soul_coin_balance += amount`` read
    the identity-mapped User, which — after an atomic charge on the same user earlier
    in the same session (stake→settle, invest→settle) — is STALE (charge uses
    synchronize_session=False). Committing that object silently overwrote the real
    balance, minting/burning coins. Returns the fresh balance, 0 if the user is gone.
    """
    if not await reward_pending(db, user_id, amount, reason):
        return 0
    await db.commit()
    row = await db.execute(select(User.soul_coin_balance).where(User.id == user_id))
    return row.scalar_one_or_none() or 0


async def reward_creator_passive(db: AsyncSession, creator_id: str | None, resident_slug: str) -> dict | None:
    """
    Award 1 SC to creator when their resident gets a conversation.
    Returns notification payload if reward given, None if the creator is one of
    the non-human sentinels (or missing).

    NON_USER_CREATOR_IDS covers both spellings: the admin console's "system"
    and the seed cast's SYSTEM_CREATOR_ID UUID. Only the former used to be
    checked, so every built-in NPC conversation minted 1 SC into the seed
    System account and inflated the admin economy panel's total_issued.
    creator_id is nullable (account deletion orphans residents).
    """
    if creator_id is None or creator_id in NON_USER_CREATOR_IDS:
        return None

    # Realism P0-5d: reuse the atomic UPDATE reward() instead of the old
    # SELECT-then-`user.soul_coin_balance += 1`, which read a possibly-stale
    # identity-mapped User (after an atomic charge earlier in the session) and
    # silently overwrote the real balance. reward() returns 0 iff no row updated
    # (creator gone) — otherwise the fresh post-credit balance (≥1).
    new_balance = await reward(db, creator_id, 1, f"creator_passive:{resident_slug}")
    if new_balance == 0:
        return None

    return {
        "type": "coin_earned",
        "amount": 1,
        "reason": "creator_passive",
        "resident_slug": resident_slug,
        "new_balance": new_balance,
    }
