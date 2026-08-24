"""Preview and atomically apply the one-time town liquidity bootstrap."""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.economy_bootstrap import (
    EconomyBootstrapBatch,
    EconomyBootstrapGrant,
)
from app.models.resident import Resident
from app.models.resident_treasury import ResidentTreasury
from app.services.civic_membership import SIM_RESIDENT_TYPES


BOOTSTRAP_KEY = "town-liquidity-v1"


def _serialize_batch(batch: EconomyBootstrapBatch) -> dict:
    return {
        "batch_id": batch.id,
        "bootstrap_key": batch.bootstrap_key,
        "resident_floor_sc": int(batch.resident_floor_sc),
        "town_target_sc": int(batch.town_target_sc),
        "town_grant_sc": int(batch.town_grant_sc),
        "summary": batch.summary_json or {},
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
        "already_applied": True,
    }


async def _residents_with_balances(
    db: AsyncSession,
) -> list[tuple[Resident, int]]:
    rows = (
        await db.execute(
            select(Resident, ResidentTreasury.balance_sc)
            .outerjoin(
                ResidentTreasury,
                ResidentTreasury.resident_slug == Resident.slug,
            )
            .where(Resident.resident_type.in_(SIM_RESIDENT_TYPES))
            .order_by(Resident.slug)
        )
    ).all()
    return [(resident, int(balance or 0)) for resident, balance in rows]


def _daily_payroll(residents: list[Resident]) -> tuple[int, int]:
    """Return a conservative ``(worker_count, daily_sc)`` planning envelope."""
    if settings.town_duty_funding_enabled:
        from app.services.duty_service import funding_source

        workers = [resident for resident in residents if funding_source(resident) == "public"]
        return len(workers), len(workers) * int(settings.town_public_duty_wage_sc or 0)
    # Before sustainable funding every productive duty can request the legacy
    # base wage.  Use all autonomous residents as the conservative runway base.
    return len(residents), len(residents) * int(settings.npc_default_wage_sc or 0)


async def preview(db: AsyncSession) -> dict:
    existing = (
        await db.execute(
            select(EconomyBootstrapBatch).where(
                EconomyBootstrapBatch.bootstrap_key == BOOTSTRAP_KEY
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _serialize_batch(existing)

    from app.services import treasury_service

    rows = await _residents_with_balances(db)
    floor = int(settings.economy_bootstrap_resident_floor_sc)
    grants = [
        {
            "resident_slug": resident.slug,
            "balance_before_sc": balance,
            "amount_sc": floor - balance,
            "balance_after_sc": floor,
        }
        for resident, balance in rows
        if balance < floor
    ]
    worker_count, daily_payroll_sc = _daily_payroll([resident for resident, _ in rows])
    town_target = daily_payroll_sc * int(settings.economy_bootstrap_payroll_days)
    town_before = await treasury_service.balance(db)
    return {
        "bootstrap_key": BOOTSTRAP_KEY,
        "resident_floor_sc": floor,
        "resident_count": len(rows),
        "resident_grants": grants,
        "resident_grant_sc": sum(grant["amount_sc"] for grant in grants),
        "payroll_worker_count": worker_count,
        "daily_payroll_sc": daily_payroll_sc,
        "payroll_days": int(settings.economy_bootstrap_payroll_days),
        "town_balance_before_sc": town_before,
        "town_target_sc": town_target,
        "town_grant_sc": max(0, town_target - town_before),
        "already_applied": False,
    }


async def apply(db: AsyncSession, *, requested_by_user_id: str) -> dict:
    plan = await preview(db)
    if plan.get("already_applied"):
        return plan

    from app.services import coin_service, treasury_service
    from app.services.duty_service import set_wallet_cache

    batch = EconomyBootstrapBatch(
        bootstrap_key=BOOTSTRAP_KEY,
        requested_by_user_id=requested_by_user_id,
        resident_floor_sc=int(plan["resident_floor_sc"]),
        town_target_sc=int(plan["town_target_sc"]),
        town_grant_sc=int(plan["town_grant_sc"]),
        summary_json={
            "resident_count": int(plan["resident_count"]),
            "resident_grant_count": len(plan["resident_grants"]),
            "resident_grant_sc": int(plan["resident_grant_sc"]),
            "payroll_worker_count": int(plan["payroll_worker_count"]),
            "daily_payroll_sc": int(plan["daily_payroll_sc"]),
            "payroll_days": int(plan["payroll_days"]),
            "town_balance_before_sc": int(plan["town_balance_before_sc"]),
        },
        created_at=datetime.now(UTC),
    )
    db.add(batch)
    try:
        await db.flush()
    except IntegrityError:
        # A second admin request may have previewed before the first committed.
        # The unique bootstrap key is the authority; turn that race into the
        # same idempotent response instead of a 500 or a second grant.
        await db.rollback()
        existing = (
            await db.execute(
                select(EconomyBootstrapBatch).where(
                    EconomyBootstrapBatch.bootstrap_key == BOOTSTRAP_KEY
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return _serialize_batch(existing)
        raise

    residents = {resident.slug: resident for resident, _ in await _residents_with_balances(db)}
    for grant in plan["resident_grants"]:
        slug = str(grant["resident_slug"])
        amount = int(grant["amount_sc"])
        before = int(grant["balance_before_sc"])
        after = int(grant["balance_after_sc"])
        await coin_service.treasury_credit_pending(
            db, slug, amount, reason=f"external_development_grant:{batch.id}"
        )
        db.add(
            EconomyBootstrapGrant(
                batch_id=batch.id,
                resident_slug=slug,
                amount_sc=amount,
                balance_before_sc=before,
                balance_after_sc=after,
            )
        )
        resident = residents.get(slug)
        if resident is not None:
            set_wallet_cache(db, resident, after)

    town_grant = int(plan["town_grant_sc"])
    if town_grant > 0:
        await treasury_service.tax_pending(
            db,
            town_grant,
            "external_development_grant",
            ref_key=f"economy-bootstrap:{BOOTSTRAP_KEY}:town",
        )
    await db.commit()
    await db.refresh(batch)
    return _serialize_batch(batch)
