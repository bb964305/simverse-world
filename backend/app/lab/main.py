"""Standalone Lab Runner entrypoint."""
from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable

import app.models  # noqa: F401
from app.config import settings
from app.database import async_session
from app.lab import (
    outbox_dispatcher,
    queue as lab_queue,
    runner as runner_module,
    terminalizer,
)
from app.observability import init_sentry
from app.redis_client import close_redis

logger = logging.getLogger(__name__)

RunnerLoop = Callable[[], Awaitable[None]]
DispatcherLoop = Callable[..., Awaitable[None]]
TerminalizerLoop = Callable[..., Awaitable[None]]


class RunnerService:
    def __init__(
        self,
        *,
        session_factory=async_session,
        runner_loop: RunnerLoop | None = None,
        protocol_version: int | None = None,
        world_reload_loop: RunnerLoop | None = None,
        dispatcher_loop: DispatcherLoop | None = None,
        terminalizer_loop: TerminalizerLoop | None = None,
        terminalizer_session_factory=None,
        terminalizer_engine=None,
    ) -> None:
        from app.lab.apply import world_reload_subscriber

        self.session_factory = session_factory
        self.protocol_version = (
            protocol_version
            if protocol_version is not None
            else runner_module.configured_protocol_version()
        )
        if runner_loop is None:
            async def configured_runner_loop() -> None:
                await runner_module.runner_loop(
                    protocol_version=self.protocol_version
                )

            self.runner_loop = configured_runner_loop
        else:
            self.runner_loop = runner_loop
        self.world_reload_loop = world_reload_loop or world_reload_subscriber
        self.dispatcher_loop = dispatcher_loop
        self.terminalizer_loop = terminalizer_loop
        self.terminalizer_session_factory = terminalizer_session_factory
        self.terminalizer_engine = terminalizer_engine
        self.ready = False
        self.failure: str | None = None
        self._ready_event = asyncio.Event()

    async def wait_ready(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self._ready_event.wait()
            return
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    async def aclose(self) -> None:
        if self.terminalizer_engine is not None:
            await self.terminalizer_engine.dispose()

    async def run(self, *, stop_event: asyncio.Event) -> None:
        # Resolve capability before creating runner/world/outbox/terminalizer
        # tasks. A flag cannot make this process advertise readiness when its
        # matching consumer implementation is absent.
        runner_module.require_protocol_handler(self.protocol_version)
        await lab_queue.require_legacy_queues_drained()
        tasks: dict[str, asyncio.Task] = {
            "runner": asyncio.create_task(self.runner_loop(), name="runner"),
            "world_reload": asyncio.create_task(
                self.world_reload_loop(), name="world_reload"
            ),
        }
        if self.dispatcher_loop is not None:
            tasks["outbox_dispatcher"] = asyncio.create_task(
                self.dispatcher_loop(
                    self.session_factory,
                    publishers=outbox_dispatcher.default_publishers(owner="lab_runner"),
                    owned_topics=outbox_dispatcher.owned_topics("lab_runner"),
                    stop_event=stop_event,
                ),
                name="outbox_dispatcher",
            )
        if self.terminalizer_loop is not None:
            tasks["terminalizer"] = asyncio.create_task(
                self.terminalizer_loop(
                    self.session_factory,
                    terminalizer_session_factory=self.terminalizer_session_factory,
                    stop_event=stop_event,
                ),
                name="terminalizer",
            )

        stop_task = asyncio.create_task(stop_event.wait(), name="stop_signal")
        self.failure = None
        self.ready = True
        self._ready_event.set()

        try:
            done, _ = await asyncio.wait(
                {stop_task, *tasks.values()},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for finished in done:
                if finished is stop_task:
                    continue
                exc = finished.exception()
                if exc is not None:
                    self.failure = f"{finished.get_name()}: {exc}"
                    raise exc
        finally:
            stop_task.cancel()
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(stop_task, *tasks.values(), return_exceptions=True)
            self.ready = False
            await self.aclose()


def build_runner_service() -> RunnerService:
    protocol_version = runner_module.configured_protocol_version()
    runner_module.require_protocol_handler(protocol_version)
    terminalizer_factory = None
    terminalizer_engine = None
    # Legacy command/event recovery is part of the default Lab Runner lifecycle.
    # The rollout gates below only admit the dedicated v2 financial consumer.
    terminalizer_loop = terminalizer.run_terminalizer_loop

    terminalizer_url = (settings.lab_terminalizer_database_url or "").strip()
    if settings.lab_terminalizer_v2_enabled:
        if not settings.lab_terminalizer_worker_enabled:
            raise RuntimeError(
                "lab_terminalizer_v2_enabled requires lab_terminalizer_worker_enabled"
            )
        if not terminalizer_url:
            raise RuntimeError(
                "lab_terminalizer_v2_enabled requires lab_terminalizer_database_url"
            )

    if settings.lab_terminalizer_worker_enabled and terminalizer_url:
        dedicated = terminalizer.build_session_factory(terminalizer_url)
        terminalizer_engine = dedicated.engine
        terminalizer_factory = dedicated.session_factory

    return RunnerService(
        session_factory=async_session,
        protocol_version=protocol_version,
        dispatcher_loop=(
            outbox_dispatcher.run_dispatch_loop
            if settings.lab_outbox_v2_enabled
            else None
        ),
        terminalizer_loop=terminalizer_loop,
        terminalizer_session_factory=terminalizer_factory,
        terminalizer_engine=terminalizer_engine,
    )


async def main() -> None:
    init_sentry("lab-runner")
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
        except NotImplementedError:  # pragma: no cover
            pass

    if not getattr(settings, "lab_enabled", True):
        logger.warning(
            "lab-runner: lab_enabled=false at deploy — staying dormant (no queue consume)"
        )
        try:
            await stop_event.wait()
        finally:
            for sig in registered:
                loop.remove_signal_handler(sig)
            await close_redis()
            logger.info("lab-runner stopped (dormant)")
        return

    # Preserve the v1 startup order while ensuring an unsupported v2 build
    # fails before world/outbox/queue work.
    if settings.lab_agent_v2_enabled:
        runner_module.require_protocol_handler(2)

    from app.lab.apply import reload_world

    try:
        await reload_world()
    except Exception:
        logger.warning("initial world overlay load skipped", exc_info=True)

    service = build_runner_service()
    logger.info("lab-runner starting")

    try:
        await service.run(stop_event=stop_event)
    finally:
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
