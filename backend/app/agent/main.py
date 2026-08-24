"""Standalone agent-worker entrypoint (P0-3 short-term).

Runs the background loops (agent behavior, heat cron, embedding backfill)
in a process separate from the API, so the API can scale to multiple
workers without duplicating agent behavior or LLM spend.

Start with: python -m app.agent.main
"""
import asyncio
import logging
import signal

import app.models  # noqa: F401 — full mapper registry so cross-table FKs resolve
from app.agent.loop import agent_loop
from app.config import settings
from app.observability import init_sentry
from app.redis_client import close_redis
from app.http import close_client
from app.tasks.embedding_backfill import embedding_backfill_loop
from app.tasks.economy_cron import economy_cron_loop
from app.tasks.caravan_lifecycle import caravan_lifecycle_loop
from app.tasks.heat_cron import heat_cron_loop
from app.tasks.event_cron import event_cron_loop
from app.tasks.loop_heartbeat import clear_owned_heartbeats
from app.tasks.nightly_cron import nightly_cron_loop
from app.tasks.resident_sprite_worker import resident_sprite_worker_loop

logger = logging.getLogger(__name__)


async def main() -> None:
    """Run all background loops until SIGTERM/SIGINT or cancellation."""
    init_sentry("agent-worker")  # no-op without SENTRY_DSN
    logger.info("agent-worker starting: world loops")
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    # Redis outlives this container.  Clear the previous owner's leases so the
    # Compose healthcheck can only pass after this process emits fresh beats.
    await clear_owned_heartbeats(
        ("agent", "event", "heat", "nightly", "embedding_backfill", "caravan", "economy")
    )

    def _request_stop(sig: signal.Signals) -> None:
        logger.info("agent-worker received %s — shutting down", sig.name)
        stop_event.set()

    registered: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop, sig)
            registered.append(sig)
        except NotImplementedError:  # pragma: no cover — non-Unix platforms
            pass

    # P3: merge the world overlay at startup + subscribe to reloads so the
    # tick's pathfinding/planning sees an approved building without a redeploy.
    from app.lab.apply import reload_world, world_reload_subscriber
    try:
        await reload_world()
    except Exception:
        logger.warning("initial world overlay load skipped", exc_info=True)

    tasks = [
        asyncio.create_task(agent_loop.run(), name="agent-loop"),
        asyncio.create_task(heat_cron_loop(), name="heat-cron"),
        asyncio.create_task(event_cron_loop(), name="event-cron"),
        asyncio.create_task(nightly_cron_loop(), name="nightly-cron"),
        asyncio.create_task(embedding_backfill_loop(), name="embedding-backfill"),
        asyncio.create_task(caravan_lifecycle_loop(), name="caravan-lifecycle"),
        asyncio.create_task(economy_cron_loop(), name="economy-cron"),
        asyncio.create_task(world_reload_subscriber(), name="world-reload"),
    ]
    if settings.resident_sprite_enabled:
        tasks.append(
            asyncio.create_task(
                resident_sprite_worker_loop(), name="resident-sprite-worker"
            )
        )
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-signal")

    try:
        pending = {stop_task, *tasks}
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task in done:
                break
            for finished in done:
                exc = finished.exception()
                if exc is not None:
                    logger.error(
                        "agent-worker task %s crashed", finished.get_name(), exc_info=exc
                    )
                    raise exc
                # The loops are infinite; a normal return (e.g. embedding
                # backfill short-circuits when EMBEDDING_ENABLED=false) is
                # unexpected but not fatal — keep the remaining loops alive.
                logger.warning(
                    "agent-worker task %s exited unexpectedly", finished.get_name()
                )
    finally:
        stop_task.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(stop_task, *tasks, return_exceptions=True)
        for sig in registered:
            loop.remove_signal_handler(sig)
        # Close the shared Redis client the loops used to publish WS events.
        await close_redis()
        await close_client()
        logger.info("agent-worker stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main())
