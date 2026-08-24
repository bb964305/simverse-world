"""World-day NPC consumption driver with a durable at-most-once day claim."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.config import settings
from app.database import async_session
from app.models.system_config import SystemConfig
from app.tasks.loop_heartbeat import beat
from app.world_clock import world_date_key

logger = logging.getLogger(__name__)
ECONOMY_CRON_INTERVAL_SECONDS = 60
_CLAIM_PREFIX = "npc_trade_world_day:"


async def _claim_world_day(db, day_key: str) -> bool:
    """Claim before spending: a crash may under-spend one day, never double-buy."""
    key = f"{_CLAIM_PREFIX}{day_key}"
    values = {
        "key": key,
        "value": datetime.now(UTC).isoformat(),
        "group": "economy",
        "updated_at": datetime.now(UTC),
        "updated_by": "economy_world_day",
    }
    dialect = db.get_bind().dialect.name
    if dialect in ("postgresql", "sqlite"):
        insert = postgresql_insert if dialect == "postgresql" else sqlite_insert
        result = await db.execute(
            insert(SystemConfig)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[SystemConfig.key])
        )
        won = (result.rowcount or 0) == 1
    else:
        existing = (
            await db.execute(select(SystemConfig.key).where(SystemConfig.key == key))
        ).scalar_one_or_none()
        won = existing is None
        if won:
            db.add(SystemConfig(**values))
    await db.commit()
    return won


async def run_world_day_consumption(*, day_key: str | None = None) -> dict:
    summary = {"day": day_key or world_date_key(), "claimed": False,
               "bought": 0, "spent": 0, "tax": 0}
    if not (
        settings.npc_economy_enabled
        and settings.npc_trade_enabled
        and settings.npc_trade_world_day_enabled
    ):
        return summary
    async with async_session() as db:
        if not await _claim_world_day(db, summary["day"]):
            return summary
        summary["claimed"] = True
        from app.services.npc_trade_service import run_consumption_pass

        result = await run_consumption_pass(db)
        summary.update(result)
    if summary["bought"]:
        logger.info(
            "world-day economy %s: %d purchases for %d SC (tax %d)",
            summary["day"], summary["bought"], summary["spent"], summary["tax"],
        )
    return summary


async def economy_cron_loop() -> None:
    while True:
        await beat("economy")
        try:
            await run_world_day_consumption()
        except Exception:
            logger.error("world-day economy pass failed", exc_info=True)
        await asyncio.sleep(ECONOMY_CRON_INTERVAL_SECONDS)
