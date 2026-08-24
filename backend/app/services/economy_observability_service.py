"""Operational snapshot for resident liquidity and economy rollout gates."""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.caravan_visit import CaravanVisit
from app.models.economy_bootstrap import EconomyBootstrapBatch
from app.models.market import CaravanMarketPurchase, LabMarketCandidate
from app.models.resident import Resident
from app.models.resident_treasury import ResidentTreasury
from app.services.civic_membership import SIM_RESIDENT_TYPES


async def snapshot(db: AsyncSession) -> dict:
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
    balances = [int(balance or 0) for _resident, balance in rows]
    reserve = int(settings.npc_trade_reserve_sc)
    import_floor = 4 + reserve
    work_floor = int(settings.npc_work_item_price_sc) + reserve

    from app.services import treasury_service
    from app.services.economy_bootstrap_service import preview as bootstrap_preview

    town_balance = await treasury_service.balance(db)
    bootstrap = await bootstrap_preview(db)
    bootstrap_summary = bootstrap.get("summary") or {}
    daily_payroll = int(
        bootstrap.get("daily_payroll_sc")
        or bootstrap_summary.get("daily_payroll_sc")
        or 0
    )
    payroll_runway = (
        round(town_balance / daily_payroll, 2) if daily_payroll > 0 else None
    )
    active_visit = (
        await db.execute(
            select(CaravanVisit)
            .where(CaravanVisit.phase.in_(("waiting", "inbound", "trading", "outbound")))
            .order_by(CaravanVisit.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    since = datetime.now(UTC) - timedelta(days=7)
    market_purchases = (
        await db.execute(
            select(func.count(CaravanMarketPurchase.id)).where(
                CaravanMarketPurchase.created_at >= since
            )
        )
    ).scalar_one()
    candidate_counts = dict(
        (
            await db.execute(
                select(LabMarketCandidate.status, func.count(LabMarketCandidate.id))
                .group_by(LabMarketCandidate.status)
            )
        ).all()
    )
    last_bootstrap = (
        await db.execute(
            select(EconomyBootstrapBatch)
            .order_by(EconomyBootstrapBatch.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    cap = max(
        int(settings.npc_trade_max_buys_per_night),
        math.ceil(len(rows) * float(settings.npc_trade_population_cap_ratio)),
    )
    poverty_count = sum(balance <= reserve for balance in balances)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "gates": {
            "npc_economy": bool(settings.npc_economy_enabled),
            "npc_trade": bool(settings.npc_trade_enabled),
            "world_day_consumption": bool(settings.npc_trade_world_day_enabled),
            "town_treasury": bool(settings.town_treasury_enabled),
            "town_ledger": bool(settings.town_ledger_enabled),
            "sustainable_public_wages": bool(settings.town_duty_funding_enabled),
            "market_hall_venue": settings.market_day_venue == "market_hall",
            "item_stock_guard": bool(settings.item_stock_guard_enabled),
            "caravan": bool(settings.caravan_enabled),
            "caravan_lifecycle": bool(settings.caravan_lifecycle_enabled),
            "player_market": bool(settings.market_player_enabled),
        },
        "residents": {
            "count": len(balances),
            "total_balance_sc": sum(balances),
            "median_balance_sc": float(median(balances)) if balances else 0.0,
            "min_balance_sc": min(balances) if balances else 0,
            "max_balance_sc": max(balances) if balances else 0,
            "poverty_line_sc": reserve,
            "poverty_count": poverty_count,
            "poverty_share": round(poverty_count / len(balances), 4) if balances else 0.0,
            "eligible_for_cheapest_import": sum(
                balance > import_floor for balance in balances
            ),
            "eligible_for_resident_work": sum(
                balance > work_floor for balance in balances
            ),
            "world_day_purchase_cap": cap,
        },
        "town": {
            "balance_sc": town_balance,
            "daily_payroll_sc": daily_payroll,
            "payroll_runway_world_days": payroll_runway,
            "target_payroll_days": int(settings.economy_bootstrap_payroll_days),
        },
        "caravan": {
            "visit_id": active_visit.id if active_visit else None,
            "phase": active_visit.phase if active_visit else None,
            "market_purchases_7d": int(market_purchases or 0),
        },
        "lab_market_candidates": {
            status: int(candidate_counts.get(status, 0))
            for status in ("pending", "approved", "rejected", "published")
        },
        "bootstrap": {
            "applied": last_bootstrap is not None,
            "batch_id": last_bootstrap.id if last_bootstrap else None,
            "created_at": (
                last_bootstrap.created_at.isoformat()
                if last_bootstrap and last_bootstrap.created_at else None
            ),
            "preview": bootstrap if last_bootstrap is None else None,
        },
        "warnings": [
            message
            for condition, message in (
                (settings.market_day_venue != "market_hall", "集市日场地尚未切换到 market_hall"),
                (settings.caravan_lifecycle_enabled and not settings.item_stock_guard_enabled,
                 "耐久商队已开但库存保护闸未显式开启"),
                (settings.market_player_enabled and not settings.caravan_lifecycle_enabled,
                 "玩家集市已开但耐久商队生命周期未开启"),
                (settings.town_duty_funding_enabled and not settings.town_treasury_enabled,
                 "可持续工资已开但镇财政未开启"),
                (payroll_runway is not None and payroll_runway < settings.economy_bootstrap_payroll_days,
                 "镇库工资续航低于目标"),
            )
            if condition
        ],
    }
