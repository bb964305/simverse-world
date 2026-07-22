"""Standalone Lab Runner entrypoint."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
from collections.abc import Awaitable, Callable
from uuid import uuid4

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
ControlLoop = Callable[..., Awaitable[None]]
ReadinessCheck = Callable[[], Awaitable[bool]]


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
        control_loop: ControlLoop | None = None,
        control_controllers: dict | None = None,
        control_owner_id: str | None = None,
        dependency_checks: dict[str, ReadinessCheck] | None = None,
        artifact_client=None,
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
        self.control_loop = control_loop
        self.control_controllers = dict(control_controllers or {})
        self.control_owner_id = control_owner_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid4()}"
        )
        self.dependency_checks = dict(dependency_checks or {})
        self.artifact_client = artifact_client
        self.ready = False
        self.failure: str | None = None
        self._ready_event = asyncio.Event()

    async def wait_ready(self, timeout: float | None = None) -> None:
        if timeout is None:
            await self._ready_event.wait()
            return
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    async def aclose(self) -> None:
        if self.artifact_client is not None:
            await self.artifact_client.aclose()
        if self.terminalizer_engine is not None:
            await self.terminalizer_engine.dispose()

    async def run(self, *, stop_event: asyncio.Event) -> None:
        # Resolve capability before creating runner/world/outbox/terminalizer
        # tasks. A flag cannot make this process advertise readiness when its
        # matching consumer implementation is absent.
        try:
            runner_module.require_protocol_handler(self.protocol_version)
            for name, check in self.dependency_checks.items():
                if not await check():
                    raise RuntimeError(
                        f"Lab production dependency is not ready: {name}"
                    )
            await lab_queue.require_legacy_queues_drained()
            if self.control_loop is not None and set(self.control_controllers) != {
                "runtime",
                "executor",
            }:
                raise RuntimeError(
                    "durable control requires exact runtime and executor controllers"
                )
        except BaseException:
            await self.aclose()
            raise
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
        if self.control_loop is not None:
            tasks["control_plane"] = asyncio.create_task(
                self.control_loop(
                    self.session_factory,
                    owner_id=self.control_owner_id,
                    controllers=self.control_controllers,
                    stop_event=stop_event,
                ),
                name="control_plane",
            )
        if self.artifact_client is not None:
            from app.lab.artifact_pipeline import run_artifact_reconciler

            tasks["artifact_reconciler"] = asyncio.create_task(
                run_artifact_reconciler(
                    self.session_factory,
                    client=self.artifact_client,
                    stop_event=stop_event,
                ),
                name="artifact_reconciler",
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


def build_runner_service(*, control_controllers: dict | None = None) -> RunnerService:
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

    dependency_checks: dict[str, ReadinessCheck] = {}
    artifact_client = None
    approved_deployment = None
    resolved_controllers = dict(control_controllers or {})
    if protocol_version == 2:
        from app.http import get_client
        from app.lab import control_plane
        from app.lab.artifact_pipeline import ArtifactPipelineClient
        from app.lab.egress_service.client import RemoteEgressClient
        from app.lab.egress_service.config import configured_runner_tools
        from app.lab.remote_executor import (
            configured_executor_controller,
            configured_remote_executor,
        )

        if settings.lab_global_admission_enabled:
            from app.lab.deployment_gate import require_d0_release_receipt

            approved_deployment = require_d0_release_receipt(
                path_value=settings.lab_d0_release_receipt_path,
                expected_receipt_sha256=settings.lab_d0_release_receipt_sha256,
                expected_request_hash=settings.lab_d0_request_hash,
                expected_source_sha=settings.lab_service_sha,
            )

        if not settings.lab_executor_enabled:
            raise RuntimeError("protocol-v2 requires the remote Executor")
        if not settings.lab_artifact_pipeline_enabled:
            raise RuntimeError("protocol-v2 requires the production Artifact pipeline")
        if not settings.lab_simverse_ref_base_url:
            raise RuntimeError("protocol-v2 requires the production Runtime endpoint")
        runtime_keyring = (
            settings.lab_runtime_auth_issuer,
            settings.lab_runtime_auth_current_kid,
            settings.lab_runtime_auth_current_key,
            settings.lab_runtime_auth_next_kid,
            settings.lab_runtime_auth_next_key,
        )
        if any(not value for value in runtime_keyring):
            raise RuntimeError("protocol-v2 Runtime auth keyring is incomplete")
        if (
            settings.lab_runtime_auth_current_kid
            == settings.lab_runtime_auth_next_kid
            or settings.lab_runtime_auth_current_key
            == settings.lab_runtime_auth_next_key
        ):
            raise RuntimeError("protocol-v2 Runtime auth keys must be distinct")

        executor_client = configured_remote_executor()
        artifact_client = ArtifactPipelineClient.from_settings()

        async def runtime_ready() -> bool:
            try:
                response = await get_client().get(
                    f"{settings.lab_simverse_ref_base_url.rstrip('/')}/readyz",
                    timeout=settings.lab_executor_request_timeout_s,
                )
                payload = response.json()
            except Exception:  # noqa: BLE001 - readiness is a fail-closed boolean
                return False
            return (
                response.status_code == 200
                and isinstance(payload, dict)
                and payload.get("ready") is True
                and payload.get("protocol_version") == 2
                and isinstance(payload.get("runtime_shard_id"), str)
                and bool(payload["runtime_shard_id"])
            )

        dependency_checks = {
            "runtime": runtime_ready,
            "executor": executor_client.ready,
            "artifact_pipeline": artifact_client.ready,
        }
        egress_tools = configured_runner_tools()
        if egress_tools:
            egress_client = RemoteEgressClient.configured()

            async def egress_ready() -> bool:
                return await egress_client.ready(
                    require_search="web.search" in egress_tools
                )

            dependency_checks["egress"] = egress_ready
        if approved_deployment is not None:
            service_endpoints = {
                "lab-runtime": settings.lab_simverse_ref_base_url.rstrip("/"),
                "lab-executor": settings.lab_executor_base_url.rstrip("/"),
                "artifact-ingest": settings.lab_artifact_ingest_base_url.rstrip("/"),
                "artifact-scanner": settings.lab_artifact_scanner_base_url.rstrip("/"),
                "artifact-cleanup": settings.lab_artifact_cleanup_base_url.rstrip("/"),
            }

            async def d0_service_identities_ready() -> bool:
                for service_name, endpoint in service_endpoints.items():
                    try:
                        response = await get_client().get(
                            f"{endpoint}/livez",
                            timeout=settings.lab_executor_request_timeout_s,
                        )
                        payload = response.json()
                    except Exception:  # noqa: BLE001 - fail-closed readiness probe
                        return False
                    if (
                        response.status_code != 200
                        or not isinstance(payload, dict)
                        or payload.get("alive") is not True
                        or payload.get("sha") != approved_deployment.source_sha
                        or payload.get("image_digest")
                        != approved_deployment.service_image_digests[service_name]
                    ):
                        return False
                return True

            dependency_checks["d0_service_identities"] = (
                d0_service_identities_ready
            )
        auto_controllers = {
            "runtime": control_plane.runtime_http_controller,
            "executor": configured_executor_controller(),
        }
        if resolved_controllers and set(resolved_controllers) != {
            "runtime",
            "executor",
        }:
            raise RuntimeError(
                "protocol-v2 control injection requires exact runtime/executor controllers"
            )
        resolved_controllers = resolved_controllers or auto_controllers

    control_loop = None
    if settings.lab_global_admission_enabled:
        if protocol_version != 2:
            raise RuntimeError(
                "lab_global_admission_enabled requires protocol_version 2"
            )
        if set(resolved_controllers) != {"runtime", "executor"}:
            raise RuntimeError(
                "lab_global_admission_enabled requires D0-provisioned Runtime "
                "and Executor controllers"
            )
        if approved_deployment is None:
            raise RuntimeError(
                "lab_global_admission_enabled requires a valid D0 release receipt"
            )
        from app.lab import control_plane

        control_loop = control_plane.run_control_loop

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
        control_loop=control_loop,
        control_controllers=resolved_controllers,
        dependency_checks=dependency_checks,
        artifact_client=artifact_client,
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
