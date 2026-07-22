"""FastAPI surface for the independent, durable OCI Executor."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.lab.protocol import (
    MAX_COMMAND_BYTES,
    ControlCommand,
    ExecutorArtifactManifest,
    ExecutorJobCommand,
    ExecutorJobResult,
    ExecutorResourceLimits,
    ServiceReceipt,
    content_digest,
)
from app.lab.runtime_ref.service_auth import (
    MAX_REQUEST_BYTES,
    RequestSchemaError,
    ServiceAuthConfig,
    ServiceAuthError,
    ServiceClaims,
    ServiceTokenValidator,
    canonical_json_bytes,
    canonical_request_digest,
    extract_bearer_token,
)
from app.lab.deployment_identity import DeploymentIdentity
from app.lab.sandbox.oci_executor import (
    ExecutorError,
    OciExecutor,
    SandboxLimits,
    SandboxOutputRequest,
    SandboxTeardownError,
    command_from_args,
    deterministic_container_name,
)

from .schemas import (
    EXECUTOR_ACTIVE_STATES,
    EXECUTOR_TERMINAL_STATES,
    ExecutorArtifactEnvelope,
    ExecutorJobResultEnvelope,
    ExecutorJobStatus,
    ReceiptSigner,
    ReceiptSignerConfig,
    deterministic_job_id,
)
from .store import (
    ArtifactUploadStage,
    ExecutorStore,
    ExecutorStoreBindingError,
    ExecutorStoreCapacity,
    ExecutorStoreConflict,
    ExecutorStoreFenced,
    ExecutorStoreNotFound,
    StoredStagedExecution,
    StoredJob,
)
from .artifact_uploader import (
    ExecutorArtifactUploadError,
    ExecutorArtifactUploader,
    ExecutorArtifactUploaderConfig,
    resolve_spool_path,
)


_MAX_JSON_DEPTH = 32


@dataclass(frozen=True)
class ExecutorServiceConfig:
    instance_id: str
    store_path: str
    image: str
    service_auth: ServiceAuthConfig
    receipt_signer: ReceiptSignerConfig
    ingest_base_url: str | None = None
    artifact_spool_path: str | None = None
    artifact_upload_timeout_seconds: float = 30.0
    deployment_identity: DeploymentIdentity | None = None
    runner: str = "docker"
    user: str = "65534:65534"
    max_concurrent_jobs: int = 4
    max_pending_jobs: int = 64
    max_limits: ExecutorResourceLimits = field(
        default_factory=lambda: ExecutorResourceLimits(
            wall_clock_ms=120_000,
            cpu_millis=2_000,
            memory_bytes=1024 * 1024 * 1024,
            pids=512,
            stdout_bytes=64 * 1024,
            stderr_bytes=64 * 1024,
            scratch_bytes=512 * 1024 * 1024,
        )
    )

    def __post_init__(self) -> None:
        if not self.instance_id or len(self.instance_id) > 100:
            raise ValueError("executor instance_id must be 1..100 characters")
        if not self.store_path:
            raise ValueError("executor store_path is required")
        if (
            not self.image
            or self.image.strip() != self.image
            or not self.runner
            or not self.user
        ):
            raise ValueError("executor image, runner, and user are required")
        if self.service_auth.audience != "lab-executor":
            raise ValueError("executor service auth audience must be lab-executor")
        if self.receipt_signer.audience != "lab-executor-receipt":
            raise ValueError(
                "executor receipt audience must be lab-executor-receipt"
            )
        if (
            type(self.max_concurrent_jobs) is not int
            or self.max_concurrent_jobs <= 0
        ):
            raise ValueError("max_concurrent_jobs must be positive")
        if (
            type(self.max_pending_jobs) is not int
            or self.max_pending_jobs < self.max_concurrent_jobs
        ):
            raise ValueError(
                "max_pending_jobs must be at least max_concurrent_jobs"
            )
        if (
            self.max_limits.stdout_bytes > 64 * 1024
            or self.max_limits.stderr_bytes > 64 * 1024
        ):
            raise ValueError("executor stream caps cannot exceed 64 KiB each")
        if self.artifact_upload_timeout_seconds <= 0:
            raise ValueError("executor artifact upload timeout must be positive")
        if self.ingest_base_url is not None and (
            not self.ingest_base_url
            or self.ingest_base_url != self.ingest_base_url.strip()
        ):
            raise ValueError("executor Ingest base URL must be canonical")
        if self.artifact_spool_path is not None and not self.artifact_spool_path:
            raise ValueError("executor artifact spool path must not be empty")


class ExecutorService:
    def __init__(self, config: ExecutorServiceConfig) -> None:
        self.config = config
        self.store = ExecutorStore(config.store_path)
        self.validator = ServiceTokenValidator(config.service_auth)
        self.signer = ReceiptSigner(config.receipt_signer)
        spool_value = config.artifact_spool_path or f"{config.store_path}.artifacts"
        self.artifact_spool_root = Path(spool_value).expanduser().resolve()
        self.artifact_uploader = (
            None
            if config.ingest_base_url is None
            else ExecutorArtifactUploader(
                ExecutorArtifactUploaderConfig(
                    ingest_base_url=config.ingest_base_url,
                    timeout_seconds=config.artifact_upload_timeout_seconds,
                )
            )
        )
        self.driver = OciExecutor(
            image=config.image,
            limits=SandboxLimits(),
            runner=config.runner,
            user=config.user,
        )
        if self.driver.configured_image_digest is None:
            raise ValueError("executor image must be pinned by sha256 digest")
        self._semaphore = asyncio.Semaphore(config.max_concurrent_jobs)
        self._tasks: dict[str, asyncio.Task] = {}
        self._running_jobs: set[str] = set()
        self._started = False
        self._closing = False

    async def start(self) -> None:
        self.artifact_spool_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.artifact_spool_root.is_symlink() or not self.artifact_spool_root.is_dir():
            raise ValueError("executor artifact spool root is invalid")
        os.chmod(self.artifact_spool_root, 0o700)
        await self.store.initialize()
        await self.store.bind_identity(
            instance_id=self.config.instance_id,
            image_digest=str(self.driver.configured_image_digest),
        )
        recoverable = await self.store.list_recoverable()
        by_action: dict[str, list[StoredJob]] = defaultdict(list)
        for job in recoverable:
            by_action[job.command.action_id].append(job)
        to_schedule: list[str] = []
        for jobs in by_action.values():
            highest_epoch = max(job.command.epoch for job in jobs)
            for job in jobs:
                staged = (
                    await self.store.get_staged_execution(job.command.job_id)
                    if job.state == "teardown_pending"
                    else None
                )
                resumable = job.state == "accepted" or (
                    job.state == "teardown_pending" and staged is not None
                )
                if resumable and job.command.epoch == highest_epoch:
                    to_schedule.append(job.command.job_id)
                else:
                    await self.reconcile_job(job, error_code="executor_restarted")
        for job_id in to_schedule:
            self.schedule(job_id)
        self._started = True

    async def stop(self) -> None:
        self._closing = True
        recoverable = await self.store.list_recoverable()
        for job in recoverable:
            task = self._tasks.get(job.command.job_id)
            if (
                job.state == "teardown_pending"
                and (
                    await self.store.get_staged_execution(job.command.job_id)
                    is not None
                    or (task is not None and not task.done())
                )
            ):
                continue
            await self.reconcile_job(job, error_code="executor_shutdown")
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._running_jobs.clear()

    def schedule(self, job_id: str) -> None:
        if self._closing:
            return
        known = self._tasks.get(job_id)
        if known is not None and not known.done():
            return
        task = asyncio.create_task(self._run_job(job_id), name=f"executor-job:{job_id}")
        self._tasks[job_id] = task

        def _done(completed: asyncio.Task) -> None:
            self._tasks.pop(job_id, None)
            self._running_jobs.discard(job_id)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(_done)

    async def ready(self) -> bool:
        pending = await self.store.count_recoverable()
        return (
            self._started
            and not self._closing
            and len(self._running_jobs) < self.config.max_concurrent_jobs
            and pending < self.config.max_pending_jobs
            and await self.store.ping()
            and await self.driver.ready()
        )

    def validate_limits(self, requested: ExecutorResourceLimits) -> None:
        maximum = self.config.max_limits
        for name in (
            "wall_clock_ms",
            "cpu_millis",
            "memory_bytes",
            "pids",
            "stdout_bytes",
            "stderr_bytes",
            "scratch_bytes",
        ):
            if getattr(requested, name) > getattr(maximum, name):
                raise HTTPException(status_code=422, detail=f"limit_exceeded:{name}")

    def validate_image(self, command: ExecutorJobCommand) -> None:
        if command.image_digest != self.driver.configured_image_digest:
            raise HTTPException(status_code=403, detail="image_digest_not_allowed")

    def validate_outputs(self, command: ExecutorJobCommand) -> None:
        if not command.outputs:
            return
        if self.artifact_uploader is None:
            raise HTTPException(
                status_code=503, detail="artifact_output_upload_not_configured"
            )
        try:
            for output in command.outputs:
                self.artifact_uploader.validate_spec(output)
        except ExecutorArtifactUploadError as exc:
            raise HTTPException(status_code=422, detail=exc.error_code) from exc

    async def fence_lower_jobs(self, command: ExecutorJobCommand) -> bool:
        lower = await self.store.list_lower_recoverable(
            action_id=command.action_id, epoch=command.epoch
        )
        clean = True
        for job in lower:
            reconciled = await self.reconcile_job(
                job, error_code="fenced_by_higher_epoch"
            )
            clean = clean and bool(
                reconciled.teardown_proof
                and reconciled.teardown_proof.get("removed") is True
            )
        return clean

    async def reconcile_job(
        self, job: StoredJob, *, error_code: str
    ) -> StoredJob:
        job = await self._settle_local_start(job)
        current, _ = await self.store.transition_job(
            job.command.job_id,
            expected_states=EXECUTOR_ACTIVE_STATES,
            new_state="killing",
            error_code=error_code,
        )
        if current.state in EXECUTOR_TERMINAL_STATES:
            return current
        proof: dict[str, Any]
        try:
            proof = await self.driver.control_container(
                current.container_name, "kill"
            )
        except SandboxTeardownError as exc:
            proof = exc.proof or {
                "removed": False,
                "name": current.container_name,
                "error": "teardown_unverified",
            }
        except Exception:
            proof = {
                "removed": False,
                "name": current.container_name,
                "error": "oci_control_failed",
            }
        return await self._complete_result(
            current,
            expected_states={"killing"},
            state="reconciliation_required",
            exit_code=None,
            stdout="",
            stderr="",
            teardown_proof=proof,
            error_code=error_code,
        )

    async def _settle_local_start(self, job: StoredJob) -> StoredJob:
        """Do not prove a container absent while this process is spawning it."""
        while job.state == "starting":
            task = self._tasks.get(job.command.job_id)
            if (
                task is None
                or task.done()
                or task is asyncio.current_task()
            ):
                break
            await asyncio.sleep(0.01)
            current = await self.store.get_job(job.command.job_id)
            if current is None:
                break
            job = current
        return job

    async def _run_job(self, job_id: str) -> None:
        async with self._semaphore:
            job = await self.store.get_job(job_id)
            if job is None:
                return
            if job.state == "teardown_pending":
                staged = await self.store.get_staged_execution(job_id)
                if staged is None:
                    return
                self._running_jobs.add(job_id)
                try:
                    await self._finish_staged_execution(job, staged)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    current = await self.store.get_job(job_id)
                    if (
                        current is not None
                        and current.state not in EXECUTOR_TERMINAL_STATES
                    ):
                        await self.reconcile_job(
                            current, error_code="artifact_upload_recovery_failed"
                        )
                return
            if job.state != "accepted":
                return
            command = job.command
            job, claimed = await self.store.transition_job(
                job_id,
                expected_states={"accepted"},
                new_state="starting",
            )
            if not claimed:
                return
            self._running_jobs.add(job_id)
            if command.deadline_at <= datetime.now(UTC):
                await self._complete_result(
                    job,
                    expected_states={"starting"},
                    state="failed",
                    exit_code=None,
                    stdout="",
                    stderr="deadline exceeded before start",
                    teardown_proof={
                        "removed": True,
                        "name": job.container_name,
                        "reason": "not_started",
                    },
                    error_code="deadline_exceeded",
                )
                return
            command_text = command_from_args(command.args)
            if command_text is None:
                await self._complete_result(
                    job,
                    expected_states={"starting"},
                    state="failed",
                    exit_code=None,
                    stdout="",
                    stderr="no executable command",
                    teardown_proof={
                        "removed": True,
                        "name": job.container_name,
                        "reason": "not_started",
                    },
                    error_code="invalid_command",
                )
                return
            limits = _sandbox_limits(command.limits)
            remaining_seconds = max(
                1,
                math.ceil(
                    (command.deadline_at - datetime.now(UTC)).total_seconds()
                ),
            )
            limits.wall_clock_s = min(limits.wall_clock_s, remaining_seconds)

            async def _started(_name: str) -> None:
                _updated, changed = await self.store.transition_job(
                    job_id,
                    expected_states={"starting"},
                    new_state="running",
                    mark_started=True,
                )
                if not changed:
                    raise ExecutorError("job was fenced before container start")

            async def _teardown_pending(_name: str) -> None:
                await self.store.transition_job(
                    job_id,
                    expected_states={"running"},
                    new_state="teardown_pending",
                )

            try:
                output_requests = tuple(
                    SandboxOutputRequest(
                        output_id=output.artifact_id,
                        relative_path=output.relative_path,
                        max_bytes=output.max_bytes,
                        required=output.required,
                    )
                    for output in command.outputs
                )
                result = await self.driver.run(
                    argv=["sh", "-c", command_text],
                    container_name=job.container_name,
                    limits=limits,
                    stdout_limit=command.limits.stdout_bytes,
                    stderr_limit=command.limits.stderr_bytes,
                    on_started=_started,
                    on_teardown_pending=_teardown_pending,
                    output_requests=output_requests,
                    output_root=(
                        self.artifact_spool_root if output_requests else None
                    ),
                )
                current = await self.store.get_job(job_id)
                if current is None:
                    return
                staged = await self._stage_sandbox_result(current, result)
                await self._finish_staged_execution(current, staged)
            except asyncio.CancelledError:
                raise
            except SandboxTeardownError as exc:
                current = await self.store.get_job(job_id)
                if current is not None and current.state not in EXECUTOR_TERMINAL_STATES:
                    await self._complete_result(
                        current,
                        expected_states=EXECUTOR_ACTIVE_STATES,
                        state="reconciliation_required",
                        exit_code=None,
                        stdout="",
                        stderr="sandbox teardown could not be verified",
                        teardown_proof=exc.proof or {
                            "removed": False,
                            "name": job.container_name,
                        },
                        error_code="teardown_unverified",
                    )
            except Exception:
                current = await self.store.get_job(job_id)
                if current is not None and current.state not in EXECUTOR_TERMINAL_STATES:
                    await self.reconcile_job(current, error_code="oci_execution_uncertain")

    def _spool_relative_path(self, value: str) -> str:
        candidate = Path(value).absolute()
        try:
            relative = candidate.relative_to(self.artifact_spool_root)
        except ValueError as exc:
            raise ExecutorArtifactUploadError(
                "artifact_spool_locator_invalid", uncertain=False
            ) from exc
        if not relative.parts:
            raise ExecutorArtifactUploadError(
                "artifact_spool_locator_invalid", uncertain=False
            )
        return relative.as_posix()

    async def _stage_sandbox_result(self, job: StoredJob, result) -> StoredStagedExecution:
        state = (
            "succeeded"
            if result.exit_code == 0
            and not result.timed_out
            and result.output_error_code is None
            else "failed"
        )
        error_code = (
            "wall_clock_exceeded"
            if result.timed_out
            else result.output_error_code
        )
        snapshot_relpath = (
            None
            if result.output_snapshot is None
            else self._spool_relative_path(result.output_snapshot)
        )
        uploads: tuple[ArtifactUploadStage, ...] = ()
        if state == "succeeded" and job.command.outputs:
            files = {item.output_id: item for item in result.output_files}
            declared = {item.artifact_id for item in job.command.outputs}
            required = {
                item.artifact_id
                for item in job.command.outputs
                if item.required
            }
            if not required.issubset(files) or not set(files).issubset(declared):
                state = "failed"
                error_code = "declared_output_set_mismatch"
            else:
                staged_items: list[ArtifactUploadStage] = []
                for spec in job.command.outputs:
                    output = files.get(spec.artifact_id)
                    if output is None:
                        continue
                    if output.relative_path != spec.relative_path:
                        state = "failed"
                        error_code = "declared_output_path_mismatch"
                        break
                    if (
                        spec.lease.expected_sha256 is not None
                        and spec.lease.expected_sha256 != output.sha256
                    ):
                        state = "failed"
                        error_code = "declared_output_sha256_mismatch"
                        break
                    staged_items.append(
                        ArtifactUploadStage(
                            spec=spec,
                            spool_relpath=self._spool_relative_path(output.host_path),
                            byte_size=output.byte_size,
                            sha256=output.sha256,
                        )
                    )
                if state == "succeeded":
                    uploads = tuple(staged_items)
        return await self.store.stage_execution(
            job.command.job_id,
            state=state,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            teardown_proof=result.teardown_proof,
            error_code=error_code,
            snapshot_relpath=snapshot_relpath,
            uploads=uploads,
        )

    async def _finish_staged_execution(
        self, job: StoredJob, staged: StoredStagedExecution
    ) -> StoredJob:
        final_state = staged.state
        final_error = staged.error_code
        if staged.state == "succeeded" and staged.uploads:
            if self.artifact_uploader is None:
                for upload in staged.uploads:
                    await self.store.record_artifact_upload_error(
                        job.command.job_id,
                        upload.spec.artifact_id,
                        error_code="artifact_output_upload_not_configured",
                    )
            else:
                for stored in staged.uploads:
                    while True:
                        upload = await self.store.mark_artifact_uploading(
                            job.command.job_id, stored.spec.artifact_id
                        )
                        if upload.state in {"completed", "failed"}:
                            break
                        try:
                            path = resolve_spool_path(
                                self.artifact_spool_root, upload.spool_relpath
                            )
                            receipt = await self.artifact_uploader.upload(
                                upload, path=path
                            )
                            await self.store.record_artifact_receipt(
                                job.command.job_id,
                                upload.spec.artifact_id,
                                receipt=receipt,
                            )
                            break
                        except ExecutorArtifactUploadError as exc:
                            if exc.uncertain:
                                await asyncio.sleep(0.5)
                                continue
                            await self.store.record_artifact_upload_error(
                                job.command.job_id,
                                upload.spec.artifact_id,
                                error_code=exc.error_code,
                            )
                            break
            refreshed = await self.store.get_staged_execution(job.command.job_id)
            if refreshed is None:
                raise ExecutorStoreConflict("staged executor result disappeared")
            staged = refreshed
            failed_upload = next(
                (item for item in staged.uploads if item.state != "completed"),
                None,
            )
            if failed_upload is not None:
                final_state = "failed"
                final_error = failed_upload.error_code or "artifact_upload_failed"

        artifact_receipts: list[dict[str, Any]] = []
        uploads_by_artifact = {
            item.spec.artifact_id: item for item in staged.uploads
        }
        for spec in job.command.outputs:
            upload = uploads_by_artifact.get(spec.artifact_id)
            if (
                upload is None
                or upload.state != "completed"
                or upload.receipt is None
                or upload.receipt_digest is None
            ):
                continue
            envelope = ExecutorArtifactEnvelope(
                manifest=ExecutorArtifactManifest(
                    artifact_id=spec.artifact_id,
                    producer_action_id=job.command.action_id,
                    kind=spec.kind,
                    expected_use=spec.expected_use,
                    title=spec.title,
                    content_type=spec.content_type,
                    original_filename=spec.original_filename,
                    required=spec.required,
                    byte_size=upload.byte_size,
                    sha256=upload.sha256,
                    upload_id=spec.lease.upload_id,
                ),
                upload_receipt=upload.receipt,
                upload_receipt_digest=upload.receipt_digest,
            )
            artifact_receipts.append(envelope.model_dump(mode="json"))

        current = await self.store.get_job(job.command.job_id)
        if current is None:
            raise ExecutorStoreNotFound("executor job not found")
        completed = await self._complete_result(
            current,
            expected_states={"teardown_pending"},
            state=final_state,
            exit_code=staged.exit_code,
            stdout=staged.stdout,
            stderr=staged.stderr,
            artifact_receipts=artifact_receipts,
            teardown_proof=staged.teardown_proof,
            error_code=final_error,
        )
        if completed.state in EXECUTOR_TERMINAL_STATES:
            await self._cleanup_staged_snapshot(staged)
        return completed

    async def _cleanup_staged_snapshot(
        self, staged: StoredStagedExecution
    ) -> None:
        if staged.snapshot_relpath is None:
            return
        parts = staged.snapshot_relpath.split("/")
        if len(parts) != 1 or parts[0] in {"", ".", ".."}:
            return
        snapshot = self.artifact_spool_root / parts[0]
        try:
            snapshot_stat = snapshot.lstat()
        except FileNotFoundError:
            return
        if snapshot.is_symlink() or not snapshot.is_dir():
            return
        await asyncio.to_thread(shutil.rmtree, snapshot, True)

    async def _complete_result(
        self,
        job: StoredJob,
        *,
        expected_states: set[str] | frozenset[str],
        state: str,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        artifact_receipts: list[dict[str, Any]] | None = None,
        teardown_proof: dict[str, Any],
        error_code: str | None,
    ) -> StoredJob:
        result_payload = {
            "schema_version": 1,
            "job_id": job.command.job_id,
            "action_id": job.command.action_id,
            "epoch": job.command.epoch,
            "state": state,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "artifact_receipts": artifact_receipts or [],
            "teardown_proof": teardown_proof,
        }
        result = ExecutorJobResult.model_validate(
            {
                **result_payload,
                "result_digest": content_digest(result_payload),
            }
        )
        receipt = self.signer.sign(
            operation_id=f"{job.command.job_id}:result",
            request_digest=job.request_digest,
            run_id=job.command.run_id,
            session_id=job.command.session_id,
            action_id=job.command.action_id,
            epoch=job.command.epoch,
            status=state,
            payload={
                "job_id": job.command.job_id,
                "state": state,
                "result_digest": result.result_digest,
                "teardown_proof_digest": content_digest(teardown_proof),
                "instance_id": self.config.instance_id,
            },
        )
        completed, _changed = await self.store.complete_job(
            job.command.job_id,
            expected_states=expected_states,
            result=result,
            receipt=receipt,
            error_code=error_code,
        )
        return completed

    async def control_job(
        self,
        job_id: str,
        command: ControlCommand,
        *,
        request_digest: str,
    ) -> ServiceReceipt:
        known = await self.store.get_job(job_id)
        if known is not None:
            await self._settle_local_start(known)
        claim = await self.store.claim_control(
            job_id, command, request_digest=request_digest
        )
        if claim.receipt is not None:
            return claim.receipt
        job = claim.job
        proof = job.teardown_proof or (
            job.result.teardown_proof if job.result is not None else None
        )
        if job.state not in EXECUTOR_TERMINAL_STATES:
            try:
                proof = await self.driver.control_container(
                    job.container_name, command.action
                )
            except SandboxTeardownError as exc:
                proof = exc.proof
            except Exception:
                proof = {
                    "removed": False,
                    "name": job.container_name,
                    "error": "oci_control_failed",
                }
            target_state = {
                "cancel": "cancelled",
                "terminate": "terminated",
                "kill": "killed",
            }[command.action]
            if not proof or proof.get("removed") is not True:
                target_state = "reconciliation_required"
            job = await self._complete_result(
                job,
                expected_states={
                    "cancelling", "terminating", "killing"
                },
                state=target_state,
                exit_code=None,
                stdout="",
                stderr="",
                teardown_proof=proof or {
                    "removed": False,
                    "name": job.container_name,
                    "error": "teardown_unverified",
                },
                error_code=(
                    None
                    if target_state != "reconciliation_required"
                    else "teardown_unverified"
                ),
            )
        elif not proof:
            try:
                proof = await self.driver.control_container(
                    job.container_name, command.action
                )
            except SandboxTeardownError as exc:
                proof = exc.proof
            except Exception:
                proof = {
                    "removed": False,
                    "name": job.container_name,
                    "error": "oci_control_failed",
                }

        confirmed = bool(proof and proof.get("removed") is True)
        receipt = self.signer.sign(
            operation_id=command.command_id,
            request_digest=request_digest,
            run_id=command.run_id,
            session_id=command.session_id,
            action_id=command.target_id,
            epoch=command.epoch,
            status=("confirmed_stopped" if confirmed else "reconciliation_required"),
            payload={
                "request_id": command.request_id,
                "job_id": job_id,
                "target_id": command.target_id,
                "action": command.action,
                "job_state": job.state,
                "teardown_proof": proof or {},
                "instance_id": self.config.instance_id,
            },
        )
        return await self.store.complete_control(
            command.command_id, receipt=receipt
        )


def _sandbox_limits(limits: ExecutorResourceLimits) -> SandboxLimits:
    mib = 1024 * 1024
    return SandboxLimits(
        memory_mb=max(1, math.ceil(limits.memory_bytes / mib)),
        cpus=max(0.001, limits.cpu_millis / 1000),
        pids=limits.pids,
        wall_clock_s=max(1, math.ceil(limits.wall_clock_ms / 1000)),
        scratch_mb=max(1, math.ceil(limits.scratch_bytes / mib)),
        network="none",
    )


def _job_status(job: StoredJob) -> ExecutorJobStatus:
    return ExecutorJobStatus(
        job_id=job.command.job_id,
        run_id=job.command.run_id,
        session_id=job.command.session_id,
        action_id=job.command.action_id,
        epoch=job.command.epoch,
        state=job.state,
        instance_id=job.instance_id,
        container_name=job.container_name,
        command_digest=job.command.command_digest,
        request_digest=job.request_digest,
        submit_receipt=job.submit_receipt,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def create_app(config: ExecutorServiceConfig) -> FastAPI:
    service = ExecutorService(config)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(
        title="Simverse Lab Executor",
        version="1.0",
        lifespan=lifespan,
    )
    app.state.executor_service = service

    def _authenticate(
        authorization: str | None, *, action: str
    ) -> ServiceClaims:
        try:
            return service.validator.validate(
                extract_bearer_token(authorization), required_action=action
            )
        except ServiceAuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.reason) from exc

    def _enforce_binding(
        claims: ServiceClaims,
        *,
        run_id: str,
        session_id: str,
        epoch: int,
    ) -> None:
        if (
            claims.run_id != run_id
            or claims.session_id != session_id
            or claims.epoch != epoch
        ):
            raise HTTPException(status_code=403, detail="binding_mismatch")

    def _enforce_command_jti(
        claims: ServiceClaims, *, action: str, command_id: str
    ) -> None:
        expected = "cmd-" + hashlib.sha256(
            canonical_json_bytes(
                {
                    "issuer": service.validator.config.issuer,
                    "audience": service.validator.config.audience,
                    "run_id": claims.run_id,
                    "session_id": claims.session_id,
                    "epoch": claims.epoch,
                    "action": action,
                    "command_id": command_id,
                }
            )
        ).hexdigest()
        if claims.jti != expected:
            raise HTTPException(status_code=403, detail="command_token_mismatch")

    async def _job_for_claims(
        job_id: str, claims: ServiceClaims
    ) -> StoredJob:
        job = await service.store.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job_not_found")
        _enforce_binding(
            claims,
            run_id=job.command.run_id,
            session_id=job.command.session_id,
            epoch=job.command.epoch,
        )
        return job

    @app.get("/livez")
    async def livez():
        payload = {
            "alive": True,
            "service": "lab-executor",
            "schema_version": 1,
            "instance_id": config.instance_id,
        }
        if config.deployment_identity is not None:
            payload.update(config.deployment_identity.health_fields())
        return payload

    @app.get("/readyz")
    async def readyz():
        ready = await service.ready()
        payload = {
            "ready": ready,
            "service": "lab-executor",
            "schema_version": 1,
            "instance_id": config.instance_id,
        }
        return payload if ready else JSONResponse(status_code=503, content=payload)

    @app.post("/v1/jobs", status_code=202, response_model=ExecutorJobStatus)
    async def submit_job(
        request: Request,
        authorization: Annotated[
            str | None, Header(alias="Authorization")
        ] = None,
    ):
        claims = _authenticate(authorization, action="executor.submit")
        body = await _strict_json_body(request, ExecutorJobCommand)
        _enforce_command_jti(
            claims, action="executor.submit", command_id=body.job_id
        )
        _enforce_binding(
            claims,
            run_id=body.run_id,
            session_id=body.session_id,
            epoch=body.epoch,
        )
        if body.job_id != deterministic_job_id(body.action_id, body.epoch):
            raise HTTPException(status_code=422, detail="invalid_deterministic_job_id")
        service.validate_limits(body.limits)
        service.validate_image(body)
        service.validate_outputs(body)
        request_digest = canonical_request_digest(body)
        container_name = deterministic_container_name(body.job_id)
        submit_receipt = service.signer.sign(
            operation_id=body.job_id,
            request_digest=request_digest,
            run_id=body.run_id,
            session_id=body.session_id,
            action_id=body.action_id,
            epoch=body.epoch,
            status="accepted",
            payload={
                "job_id": body.job_id,
                "state": "accepted",
                "command_digest": body.command_digest,
                "instance_id": config.instance_id,
                "container_name": container_name,
            },
        )
        try:
            job, is_retry = await service.store.accept_job(
                body,
                request_digest=request_digest,
                instance_id=config.instance_id,
                container_name=container_name,
                submit_receipt=submit_receipt,
                max_pending_jobs=config.max_pending_jobs,
            )
        except ExecutorStoreCapacity as exc:
            raise HTTPException(
                status_code=429, detail="executor_capacity_exhausted"
            ) from exc
        except ExecutorStoreFenced as exc:
            receipt = service.signer.sign(
                operation_id=body.job_id,
                request_digest=request_digest,
                run_id=body.run_id,
                session_id=body.session_id,
                action_id=body.action_id,
                epoch=body.epoch,
                status="fenced",
                payload={"highest_epoch": exc.highest_epoch},
            )
            return JSONResponse(
                status_code=409,
                content=receipt.model_dump(mode="json"),
            )
        except ExecutorStoreConflict as exc:
            raise HTTPException(status_code=409, detail="job_digest_conflict") from exc
        if not is_retry:
            fenced = await service.fence_lower_jobs(body)
            if not fenced:
                await service.reconcile_job(
                    job, error_code="prior_epoch_teardown_unverified"
                )
            else:
                service.schedule(body.job_id)
            job = await service.store.get_job(body.job_id) or job
        return _job_status(job)

    @app.get("/v1/jobs/{job_id}", response_model=ExecutorJobStatus)
    async def get_job(
        job_id: str,
        authorization: Annotated[
            str | None, Header(alias="Authorization")
        ] = None,
    ):
        claims = _authenticate(authorization, action="executor.status")
        _enforce_command_jti(
            claims, action="executor.status", command_id=f"{job_id}:status"
        )
        return _job_status(await _job_for_claims(job_id, claims))

    @app.get(
        "/v1/jobs/{job_id}/result",
        response_model=ExecutorJobResultEnvelope,
    )
    async def get_result(
        job_id: str,
        authorization: Annotated[
            str | None, Header(alias="Authorization")
        ] = None,
    ):
        claims = _authenticate(authorization, action="executor.result")
        _enforce_command_jti(
            claims,
            action="executor.result",
            command_id=f"{job_id}:result.read",
        )
        job = await _job_for_claims(job_id, claims)
        if job.state not in EXECUTOR_TERMINAL_STATES:
            raise HTTPException(status_code=409, detail="result_pending")
        if job.result is None or job.result_receipt is None:
            raise HTTPException(status_code=503, detail="result_reconciliation_required")
        return ExecutorJobResultEnvelope(
            result=job.result,
            receipt=job.result_receipt,
        )

    @app.post(
        "/v1/jobs/{job_id}/control",
        response_model=ServiceReceipt,
    )
    async def control_job(
        job_id: str,
        request: Request,
        authorization: Annotated[
            str | None, Header(alias="Authorization")
        ] = None,
    ):
        claims = _authenticate(authorization, action="executor.control")
        body = await _strict_json_body(request, ControlCommand)
        _enforce_command_jti(
            claims, action="executor.control", command_id=body.command_id
        )
        _enforce_binding(
            claims,
            run_id=body.run_id,
            session_id=body.session_id,
            epoch=body.epoch,
        )
        request_digest = canonical_request_digest(body)
        try:
            return await service.control_job(
                job_id, body, request_digest=request_digest
            )
        except ExecutorStoreNotFound as exc:
            raise HTTPException(status_code=404, detail="job_not_found") from exc
        except ExecutorStoreBindingError as exc:
            raise HTTPException(status_code=403, detail="binding_mismatch") from exc
        except ExecutorStoreFenced as exc:
            receipt = service.signer.sign(
                operation_id=body.command_id,
                request_digest=request_digest,
                run_id=body.run_id,
                session_id=body.session_id,
                action_id=body.target_id,
                epoch=body.epoch,
                status="fenced",
                payload={"highest_epoch": exc.highest_epoch},
            )
            return JSONResponse(
                status_code=409,
                content=receipt.model_dump(mode="json"),
            )
        except ExecutorStoreConflict as exc:
            raise HTTPException(status_code=409, detail="control_digest_conflict") from exc

    return app


async def _strict_json_body(request: Request, model_type):
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid_content_length") from exc
        if declared < 0:
            raise HTTPException(status_code=400, detail="invalid_content_length")
        if declared > MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="request_body_too_large")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="request_body_too_large")
    try:
        value = json.loads(
            bytes(raw),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite JSON")
            ),
        )
        _enforce_json_depth(value)
        body = model_type.model_validate(value)
        canonical_json_bytes(body, max_bytes=MAX_COMMAND_BYTES)
        return body
    except RequestSchemaError as exc:
        raise HTTPException(status_code=413, detail="request_body_too_large") from exc
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        RecursionError,
    ) as exc:
        raise HTTPException(status_code=422, detail="invalid_request_body") from exc


def _reject_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _enforce_json_depth(value: Any) -> None:
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > _MAX_JSON_DEPTH:
            raise ValueError("request JSON exceeds depth cap")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)
