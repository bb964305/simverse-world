"""World event cron (S1): flip is_active on schedule and broadcast transitions.

Mirrors heat_cron: runs only where run_background_tasks is true (single instance
— the agent-worker in split mode), and broadcasts via manager, which fans out to
every API worker's sockets over Redis pub/sub.
"""

import asyncio
import logging

from app.config import settings
from app.database import async_session
from app.services.world_event_service import flip_active_events, write_collective_memories
from app.tasks.loop_heartbeat import beat
from app.ws.manager import manager

logger = logging.getLogger(__name__)
EVENT_CRON_INTERVAL_SECONDS = 60


async def event_cron_loop():
    """Background task: every 60s flip active world events and broadcast."""
    while True:
        try:
            async with async_session() as db:
                # E6: keep a weather segment scheduled. Created inactive with
                # starts_at=now, so the flip below activates + broadcasts it in
                # this same pass through the S1 pipeline (A2 template pattern).
                try:
                    from app.tasks.weather import ensure_weather_event
                    scheduled = await ensure_weather_event(db)
                    if scheduled is not None:
                        logger.info("Event cron: scheduled weather '%s'", scheduled.title)
                except Exception:
                    logger.warning("E6 weather step failed", exc_info=True)
                changes = await flip_active_events(db)
                # A2: on start, give active residents a shared collective memory.
                for event, phase in changes:
                    if phase == "start":
                        try:
                            await write_collective_memories(db, event)
                        except Exception:
                            logger.warning("collective memory write failed", exc_info=True)
                        # M-A C4: 集市日开场 = 外来商队进镇。判据(market_day,与
                        # shop_service._market_discount 同源)留在调用点,服务只管
                        # 到访本身；准入值由 binding policy 读取，env 只作政策存储
                        # 门关闭/缺行时的回滚 fallback。
                        if (settings.npc_economy_enabled
                                and (event.get("payload_json") or {}).get("market_day")):
                            try:
                                if settings.caravan_lifecycle_enabled:
                                    # Wake only: the leased state-machine owns
                                    # admission, movement and every money step.
                                    from app.services.caravan_lifecycle_service import (
                                        ensure_visit_for_event,
                                    )
                                    await ensure_visit_for_event(db, event)
                                else:
                                    # Dark-launch rollback path: unchanged
                                    # one-shot semantics until the lifecycle gate opens.
                                    from app.services.caravan_service import (
                                        is_caravan_enabled, run_caravan_visit,
                                    )
                                    if await is_caravan_enabled(db):
                                        visit = await run_caravan_visit(db, event)
                                        if (visit["bought"] or visit["fee"]
                                                or visit["imported"]):
                                            logger.info(
                                                "Event cron: caravan bought=%d spent=%d "
                                                "tax=%d fee=%d imported=%d",
                                                visit["bought"], visit["spent"],
                                                visit["tax"], visit["fee"],
                                                visit["imported"])
                            except Exception:
                                logger.warning("C4 caravan visit step failed", exc_info=True)
                                # 这一轮的 session 下面还要给 C3/E3 用：写了半截不
                                # 回滚，PendingRollbackError 会顺着传染并被误算到
                                # 那两段头上（同 :69-77 踩过的坑）。
                                try:
                                    await db.rollback()
                                except Exception:
                                    logger.warning("C4 step rollback itself failed",
                                                   exc_info=True)
                    # M3 F3.3: a public lecture ending spawns a resident debate.
                    if phase == "end":
                        if (settings.caravan_lifecycle_enabled
                                and (event.get("payload_json") or {}).get("market_day")):
                            try:
                                from app.services.caravan_lifecycle_service import (
                                    wake_visit_for_event,
                                )
                                await wake_visit_for_event(db, event)
                            except Exception:
                                logger.warning(
                                    "caravan close wake failed", exc_info=True,
                                )
                                try:
                                    await db.rollback()
                                except Exception:
                                    logger.warning(
                                        "caravan close wake rollback failed", exc_info=True,
                                    )
                        try:
                            from app.services.civic_service import maybe_spawn_lecture_debate
                            if await maybe_spawn_lecture_debate(db, event):
                                logger.info("Spawned lecture debate from '%s'", event.get("title"))
                        except Exception:
                            logger.warning("lecture debate step failed", exc_info=True)
                # C3: fire due script acts + settle finished seasons.
                try:
                    from app.services.script_service import (
                        fire_due_scripts, settle_due_seasons, ensure_active_season,
                    )
                    fired = await fire_due_scripts(db)
                    settled = await settle_due_seasons(db)
                    # E7: 结算之后补开下一季 —— 顺序不能反，否则刚开的季会被
                    # 同一轮的 settle 扫到（它按 ends_at 判，新季不会中，但把
                    # 开季放在结算前会让「一季结束到下一季开始」多等 60s）。
                    opened = await ensure_active_season(db)
                    if fired:
                        logger.info("Event cron: fired %d script act(s)", len(fired))
                    if settled:
                        logger.info("Event cron: settled %d season(s)", len(settled))
                    if opened is not None:
                        logger.info("Event cron: opened season %s", opened.title)
                except Exception:
                    logger.warning("C3 script/season cron step failed", exc_info=True)
                    # A failed commit above leaves the shared session in
                    # PendingRollbackError; without a rollback the next
                    # statement on this session (drive_due_debates' select
                    # below) raises immediately and gets misattributed to the
                    # E3 except block, sending debugging the wrong way.
                    try:
                        await db.rollback()
                    except Exception:
                        logger.warning("C3 step rollback itself failed", exc_info=True)
                # E3: 推进辩论生命周期（announced → live → voting → settled）。
                # run_live/settle 此前在 app/ 下零调用方，押注币会被永久冻结。
                try:
                    from app.services.debate_service import drive_due_debates
                    moved = await drive_due_debates(db)
                    if any(moved.values()):
                        logger.info("Event cron: debates live=%d settled=%d refunded=%d",
                                    moved["live"], moved["settled"], moved["refunded"])
                except Exception:
                    logger.warning("E3 debate driver step failed", exc_info=True)
                    try:
                        await db.rollback()
                    except Exception:
                        logger.warning("E3 step rollback itself failed", exc_info=True)
            for event, phase in changes:
                await manager.broadcast({
                    "type": "world_event",
                    "event": event,
                    "phase": phase,
                })
            if changes:
                logger.info("Event cron: %d world-event transitions", len(changes))
        except Exception as e:
            logger.error(f"Event cron error: {e}", exc_info=True)
        await beat("event")  # P2: liveness signal + sibling-loop watchdog
        await asyncio.sleep(EVENT_CRON_INTERVAL_SECONDS)
