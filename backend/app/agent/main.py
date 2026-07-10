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
from app.redis_client import close_redis
from app.tasks.embedding_backfill import embedding_backfill_loop
from app.tasks.heat_cron import heat_cron_loop
from app.tasks.event_cron import event_cron_loop
from app.tasks.nightly_cron import nightly_cron_loop

logger = logging.getLogger(__name__)


async def main() -> None:
    """Run all background loops until SIGTERM/SIGINT or cancellation."""
    logger.info(
        "agent-worker starting: agent_loop + heat_cron_loop + embedding_backfill_loop"
    )
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

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

    tasks = [
        asyncio.create_task(agent_loop.run(), name="agent-loop"),
        asyncio.create_task(heat_cron_loop(), name="heat-cron"),
        asyncio.create_task(event_cron_loop(), name="event-cron"),
        asyncio.create_task(nightly_cron_loop(), name="nightly-cron"),
        asyncio.create_task(embedding_backfill_loop(), name="embedding-backfill"),
    ]
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-signal")

    try:
        done, _ = await asyncio.wait(
            {stop_task, *tasks}, return_when=asyncio.FIRST_COMPLETED
        )
        for finished in done:
            if finished is stop_task:
                continue
            exc = finished.exception()
            if exc is not None:
                logger.error(
                    "agent-worker task %s crashed", finished.get_name(), exc_info=exc
                )
                raise exc
            # The loops are infinite; a normal return is unexpected but not fatal.
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
        logger.info("agent-worker stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main())
