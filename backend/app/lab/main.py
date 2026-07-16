"""Standalone Lab Runner entrypoint (spec §5.1).

Runs the queue consume loop in a process separate from the API / agent-worker,
so a long-lived, isolation-heavy, possibly-crashing real sandbox never blocks
request handling or resident ticks.

Start with: python -m app.lab.main
"""
import asyncio
import logging
import signal

import app.models  # noqa: F401 — full mapper registry so cross-table FKs resolve
from app.lab.runner import runner_loop
from app.observability import init_sentry
from app.redis_client import close_redis

logger = logging.getLogger(__name__)


async def main() -> None:
    init_sentry("lab-runner")  # no-op without SENTRY_DSN
    logger.info("lab-runner starting: runner_loop")
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_stop(sig: signal.Signals) -> None:
        logger.info("lab-runner received %s — shutting down", sig.name)
        stop_event.set()

    registered: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop, sig)
            registered.append(sig)
        except NotImplementedError:  # pragma: no cover — non-Unix platforms
            pass

    # P3: the runner may emit proposals + apply them via the shared engine; keep
    # its LOCATIONS in sync by subscribing to reload signals too.
    from app.lab.apply import reload_world, world_reload_subscriber
    try:
        await reload_world()
    except Exception:
        logger.warning("initial world overlay load skipped", exc_info=True)

    runner_task = asyncio.create_task(runner_loop(), name="lab-runner-loop")
    world_reload_task = asyncio.create_task(world_reload_subscriber(), name="world-reload")
    stop_task = asyncio.create_task(stop_event.wait(), name="stop-signal")

    try:
        done, _ = await asyncio.wait(
            {runner_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for finished in done:
            if finished is runner_task:
                exc = finished.exception()
                if exc is not None:
                    logger.error("lab-runner loop crashed", exc_info=exc)
                    raise exc
    finally:
        stop_task.cancel()
        runner_task.cancel()
        world_reload_task.cancel()
        await asyncio.gather(stop_task, runner_task, world_reload_task, return_exceptions=True)
        for sig in registered:
            loop.remove_signal_handler(sig)
        await close_redis()
        logger.info("lab-runner stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main())
