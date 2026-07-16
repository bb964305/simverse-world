from datetime import datetime, UTC

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user import User
from app.models.transaction import Transaction
from app.models.coin_hold import CoinHold
from app.models.resident_treasury import ResidentTreasury


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


async def transfer(db: AsyncSession, from_user: str, to_user: str, amount: int, reason: str) -> bool:
    """Atomic P2P: debit ``from_user`` (guarded) then credit ``to_user``. Both
    legs + both ledger rows commit together, so a failed debit leaves nothing."""
    if amount <= 0 or from_user == to_user:
        return False
    debited = await db.execute(
        update(User)
        .where(User.id == from_user, User.soul_coin_balance >= amount)
        .values(soul_coin_balance=User.soul_coin_balance - amount)
        .execution_options(synchronize_session=False)
    )
    if debited.rowcount == 0:
        # Nothing written (guard matched 0 rows) → no rollback: rollback would
        # expire the caller's ORM objects (see charge()). Only the credit-failed
        # branch below rolls back, because there the debit really did write.
        return False
    credited = await db.execute(
        update(User)
        .where(User.id == to_user)
        .values(soul_coin_balance=User.soul_coin_balance + amount)
        .execution_options(synchronize_session=False)
    )
    if credited.rowcount == 0:
        # recipient doesn't exist — abort the whole transfer
        await db.rollback()
        return False
    db.add(Transaction(user_id=from_user, amount=-amount, reason=reason))
    db.add(Transaction(user_id=to_user, amount=amount, reason=reason))
    await db.commit()
    return True


async def hold(db: AsyncSession, user_id: str, amount: int, reason: str) -> str | None:
    """Freeze ``amount`` from a user into a CoinHold (escrow). Debit + hold-row +
    ledger commit atomically. Returns hold_id, or None if insufficient funds."""
    if amount <= 0:
        return None
    debited = await db.execute(
        update(User)
        .where(User.id == user_id, User.soul_coin_balance >= amount)
        .values(soul_coin_balance=User.soul_coin_balance - amount)
        .execution_options(synchronize_session=False)
    )
    if debited.rowcount == 0:
        # Nothing written → no rollback (would expire the caller's ORM objects,
        # e.g. the freshly-created LabTask in lab_task_service). See charge().
        return None
    h = CoinHold(user_id=user_id, amount=amount, reason=reason, status="held")
    db.add(h)
    db.add(Transaction(user_id=user_id, amount=-amount, reason=f"hold:{reason}"))
    await db.commit()
    await db.refresh(h)
    return h.id


async def settle(db: AsyncSession, hold_id: str, splits: list[tuple[str, int, str]]) -> None:
    """Distribute a held amount. Each split is (recipient, amount, reason) where
    recipient is a real user_id, ``"treasury:<slug>"``, or ``"sink"`` (consumed,
    not redistributed — the platform fee). Enforces the conservation invariant
    ``sum(split amounts) == hold.amount`` so we never mint/burn coins.
    """
    h = await db.get(CoinHold, hold_id)
    if h is None or h.status != "held":
        raise CoinError(f"hold {hold_id} not settleable (missing or not held)")
    total = sum(a for _, a, _ in splits)
    if total != h.amount:
        raise CoinError(f"settle splits sum {total} != hold amount {h.amount}")
    for recipient, amt, reason in splits:
        if amt <= 0:
            continue
        if recipient == "sink" or recipient is None:
            continue  # consumed: the original hold-charge already left circulation
        elif recipient.startswith("treasury:"):
            await treasury_credit(db, recipient[len("treasury:"):], amt, reason)
        else:
            await reward(db, recipient, amt, reason)
    h.status = "settled"
    h.settled_at = datetime.now(UTC)
    await db.commit()


async def refund(db: AsyncSession, hold_id: str, reason: str) -> None:
    """Return a held amount in full to the issuer."""
    h = await db.get(CoinHold, hold_id)
    if h is None or h.status != "held":
        raise CoinError(f"hold {hold_id} not refundable (missing or not held)")
    await reward(db, h.user_id, h.amount, reason)
    h.status = "refunded"
    h.settled_at = datetime.now(UTC)
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
    res = await db.execute(
        update(ResidentTreasury)
        .where(ResidentTreasury.resident_slug == slug)
        .values(balance_sc=ResidentTreasury.balance_sc + amount)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount == 0:
        db.add(ResidentTreasury(resident_slug=slug, balance_sc=amount))
        try:
            await db.commit()
            return
        except IntegrityError:
            # Race: another credit inserted the row first — fall through to update.
            await db.rollback()
            await db.execute(
                update(ResidentTreasury)
                .where(ResidentTreasury.resident_slug == slug)
                .values(balance_sc=ResidentTreasury.balance_sc + amount)
                .execution_options(synchronize_session=False)
            )
    await db.commit()


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
    result = await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(soul_coin_balance=User.soul_coin_balance + amount)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount == 0:
        return 0
    db.add(Transaction(user_id=user_id, amount=amount, reason=reason))
    await db.commit()
    row = await db.execute(select(User.soul_coin_balance).where(User.id == user_id))
    return row.scalar_one_or_none() or 0


async def reward_creator_passive(db: AsyncSession, creator_id: str, resident_slug: str) -> dict | None:
    """
    Award 1 SC to creator when their resident gets a conversation.
    Returns notification payload if reward given, None if creator is 'system' or not found.
    """
    if creator_id == "system":
        return None

    result = await db.execute(select(User).where(User.id == creator_id))
    user = result.scalar_one_or_none()
    if not user:
        return None

    user.soul_coin_balance += 1
    db.add(Transaction(user_id=creator_id, amount=1, reason=f"creator_passive:{resident_slug}"))
    await db.commit()

    return {
        "type": "coin_earned",
        "amount": 1,
        "reason": "creator_passive",
        "resident_slug": resident_slug,
        "new_balance": user.soul_coin_balance,
    }
