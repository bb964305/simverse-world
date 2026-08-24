"""Entrypoint for the independently deployed hosted-Agent worker."""

from __future__ import annotations

import asyncio
import logging
import signal

import app.models  # noqa: F401
from app.hosted_agents.worker import HostedAgentWorker
from app.observability import init_sentry
from app.redis_client import close_redis
from app.tasks.loop_heartbeat import clear_owned_heartbeats


logger = logging.getLogger(__name__)


async def main() -> None:
    init_sentry("hosted-agent-worker")
    await clear_owned_heartbeats(("hosted_agent",))
    worker = HostedAgentWorker()
    task = asyncio.create_task(worker.run(), name="hosted-agent-loop")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop(sig: signal.Signals) -> None:
        logger.info("hosted-agent-worker received %s", sig.name)
        stop.set()

    registered: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_stop, sig)
            registered.append(sig)
        except NotImplementedError:  # pragma: no cover
            pass
    try:
        done, _pending = await asyncio.wait(
            {task, asyncio.create_task(stop.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if task in done:
            task.result()
    finally:
        await worker.aclose()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for sig in registered:
            loop.remove_signal_handler(sig)
        await close_redis()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main())
