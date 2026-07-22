"""Durable asynchronous scan jobs and clean exact-version promotion."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.lab.artifact_services.canonical import canonical_digest
from app.lab.artifact_services.receipts import ReceiptSigner
from app.lab.artifact_services.scanner.policy import PolicyResult, ScanPolicy
from app.lab.artifact_services.schemas import ObjectRef, ScanCommand, ScanReceipt
from app.lab.artifact_services.storage.base import ObjectStorage, StorageError
from app.lab.artifact_services.store import (
    OperationConflict,
    OperationRecord,
    OperationStore,
)


logger = logging.getLogger(__name__)


class ScannerError(RuntimeError):
    status_code = 400

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class ScannerConflict(ScannerError):
    status_code = 409


class ScannerNotFound(ScannerError):
    status_code = 404


class ScannerUnavailable(ScannerError):
    status_code = 503


@dataclass(frozen=True)
class ScannerConfig:
    service_instance_id: str
    released_bucket: str
    work_dir: Path
    max_object_bytes: int
    max_attempts: int = 3
    retry_backoff_seconds: float = 5.0
    claim_seconds: int = 300
    poll_seconds: float = 1.0
    policy_timeout_seconds: float = 120.0

    def __post_init__(self) -> None:
        if not self.service_instance_id or not self.released_bucket:
            raise ValueError("scanner instance ID and released bucket are required")
        if self.max_object_bytes <= 0 or self.max_attempts <= 0:
            raise ValueError("scanner byte and attempt limits must be positive")
        if (
            self.claim_seconds <= 0
            or self.poll_seconds <= 0
            or self.policy_timeout_seconds <= 0
            or self.retry_backoff_seconds < 0
        ):
            raise ValueError("scanner timing configuration is invalid")
        if self.claim_seconds < self.policy_timeout_seconds + 60:
            raise ValueError(
                "scanner claim must cover policy timeout and storage finalization"
            )


def _stable_receipt_id(job_id: str, status: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"simverse:artifact-scan:{job_id}:{status}"))


def _released_key(command: ScanCommand) -> str:
    def segment(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]

    return "/".join(
        [
            segment(command.tenant_id),
            segment(command.run_id),
            segment(command.artifact_id),
            segment(command.scan_job_id),
            command.sha256,
        ]
    )


class ScannerService:
    def __init__(
        self,
        *,
        config: ScannerConfig,
        store: OperationStore,
        storage: ObjectStorage,
        policy: ScanPolicy,
        receipt_signer: ReceiptSigner,
    ) -> None:
        self.config = config
        self.store = store
        self.storage = storage
        self.policy = policy
        self.receipt_signer = receipt_signer
        self.owner_id = f"{socket.gethostname()}:{os.getpid()}:{config.service_instance_id}"

    async def initialize(self) -> None:
        self.config.work_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.config.work_dir, 0o700)
        await self.store.initialize()

    async def ready(self) -> bool:
        try:
            if (
                not self.policy.ready()
                or not await self.store.ready()
                or not await self.storage.ready()
            ):
                return False
            fd, path = tempfile.mkstemp(prefix=".ready-", dir=self.config.work_dir)
            os.close(fd)
            Path(path).unlink()
            return True
        except Exception:
            return False

    def _receipt(
        self,
        command: ScanCommand,
        *,
        status: str,
        released_ref: ObjectRef | None = None,
        error_code: str | None = None,
        occurred_at: datetime | None = None,
    ) -> ScanReceipt:
        moment = occurred_at or datetime.now(UTC)
        terminal = status in {"clean", "flagged", "failed"}
        outcome = {
            "status": status,
            "quarantine_ref": command.quarantine_ref.model_dump(mode="json"),
            "released_ref": None if released_ref is None else released_ref.model_dump(mode="json"),
            "policy_version": command.policy_version,
            "engine_version": self.policy.config.engine_version,
            "error_code": error_code,
        }
        return self.receipt_signer.sign(
            ScanReceipt,
            {
                "receipt_type": "artifact.scan",
                "receipt_id": _stable_receipt_id(command.scan_job_id, status),
                "service_instance_id": self.config.service_instance_id,
                "command_id": command.command_id,
                "action": "artifact.scan",
                "request_digest": canonical_digest(command),
                "tenant_id": command.tenant_id,
                "run_id": command.run_id,
                "session_id": command.session_id,
                "artifact_id": command.artifact_id,
                "producer_action_id": command.producer_action_id,
                "epoch": command.epoch,
                "status": status,
                "occurred_at": moment,
                "payload_digest": canonical_digest(outcome),
                "scan_job_id": command.scan_job_id,
                "policy_version": command.policy_version,
                "quarantine_ref": command.quarantine_ref,
                "released_ref": released_ref,
                "scan_engine_version": self.policy.config.engine_version,
                "completed_at": moment if terminal else None,
                "error_code": error_code,
            },
        )

    async def _receipt_for_existing_submit(
        self,
        *,
        record: OperationRecord,
        command_digest: str,
    ) -> ScanReceipt:
        if record.kind != "scan" or record.command_digest != command_digest:
            raise ScannerConflict("scan_job_id_conflict")
        if record.response is not None:
            return ScanReceipt.model_validate(record.response)
        if record.state != "pending":
            raise ScannerConflict("scan_job_state_invalid")
        stored_command = ScanCommand.model_validate(record.command)
        if canonical_digest(stored_command) != command_digest:
            raise ScannerConflict("scan_job_command_corrupt")
        receipt = self._receipt(stored_command, status="pending")
        try:
            await self.store.set_response(
                record.operation_id,
                state="pending",
                response=receipt.model_dump(mode="json"),
                progress={"attempts": 0},
                expected_states=("pending",),
            )
            return receipt
        except OperationConflict:
            refreshed = await self.store.get(record.operation_id)
            if (
                refreshed is not None
                and refreshed.kind == "scan"
                and refreshed.command_digest == command_digest
                and refreshed.response is not None
            ):
                return ScanReceipt.model_validate(refreshed.response)
            raise ScannerConflict("scan_job_state_changed")

    async def submit(self, command: ScanCommand) -> ScanReceipt:
        command_digest = canonical_digest(command)
        existing = await self.store.get(command.scan_job_id)
        if existing is not None:
            return await self._receipt_for_existing_submit(
                record=existing,
                command_digest=command_digest,
            )

        now = datetime.now(UTC)
        if not self.policy.ready():
            raise ScannerUnavailable("malware_scanner_unavailable")
        if command.deadline_at <= now:
            raise ScannerError("scan_deadline_expired")
        if command.byte_size > self.config.max_object_bytes:
            raise ScannerError("scan_object_too_large")
        if command.policy_version != self.policy.config.policy_version:
            raise ScannerError("scan_policy_version_mismatch")
        try:
            claim = await self.store.create_or_get(
                operation_id=command.scan_job_id,
                kind="scan",
                command_digest=command_digest,
                command=command.model_dump(mode="json"),
                initial_state="pending",
            )
        except OperationConflict as exc:
            raise ScannerConflict("scan_job_id_conflict") from exc
        return await self._receipt_for_existing_submit(
            record=claim.record,
            command_digest=command_digest,
        )

    async def get_receipt(self, scan_job_id: str) -> ScanReceipt:
        record = await self.store.get(scan_job_id)
        if record is None or record.kind != "scan" or record.response is None:
            raise ScannerNotFound("scan_job_not_found")
        return ScanReceipt.model_validate(record.response)

    async def _finish(
        self,
        command: ScanCommand,
        *,
        status: str,
        released_ref: ObjectRef | None,
        error_code: str | None,
        progress: dict,
        owner: str,
    ) -> ScanReceipt:
        receipt = self._receipt(
            command,
            status=status,
            released_ref=released_ref,
            error_code=error_code,
        )
        await self.store.set_response(
            command.scan_job_id,
            state=status,
            response=receipt.model_dump(mode="json"),
            progress=progress,
            error_code=error_code,
            expected_states=("running",),
            owner=owner,
        )
        return receipt

    async def _retry_or_fail(
        self,
        command: ScanCommand,
        *,
        attempts: int,
        error_code: str,
        progress: dict,
        owner: str,
    ) -> ScanReceipt:
        if attempts >= self.config.max_attempts:
            return await self._finish(
                command,
                status="failed",
                released_ref=None,
                error_code=error_code,
                progress={**progress, "attempts": attempts},
                owner=owner,
            )
        retry_at = datetime.now(UTC) + timedelta(seconds=self.config.retry_backoff_seconds)
        receipt = self._receipt(command, status="pending")
        await self.store.set_response(
            command.scan_job_id,
            state="pending",
            response=receipt.model_dump(mode="json"),
            progress={
                **progress,
                "attempts": attempts,
                "next_retry_at": retry_at.isoformat(),
            },
            error_code=error_code,
            expected_states=("running",),
            owner=owner,
        )
        return receipt

    async def process(self, scan_job_id: str) -> ScanReceipt | None:
        claim_owner = f"{self.owner_id}:{uuid.uuid4()}"
        record = await self.store.get(scan_job_id)
        if record is None or record.kind != "scan":
            raise ScannerNotFound("scan_job_not_found")
        if record.state in {"clean", "flagged", "failed"}:
            return ScanReceipt.model_validate(record.response)
        retry_at = record.progress.get("next_retry_at")
        if retry_at and datetime.fromisoformat(retry_at) > datetime.now(UTC):
            return None
        claimed = await self.store.claim(
            scan_job_id,
            owner=claim_owner,
            eligible_states=("pending", "running"),
            claimed_state="running",
            lease_seconds=self.config.claim_seconds,
        )
        if claimed is None:
            return None
        command = ScanCommand.model_validate(claimed.command)
        attempts = int(claimed.progress.get("attempts", 0)) + 1
        running_receipt = self._receipt(command, status="running")
        await self.store.set_response(
            scan_job_id,
            state="running",
            response=running_receipt.model_dump(mode="json"),
            progress={**claimed.progress, "attempts": attempts},
            expected_states=("running",),
            clear_claim=False,
            owner=claim_owner,
        )
        input_fd, input_name = tempfile.mkstemp(prefix="scan-in-", dir=self.config.work_dir)
        output_fd, output_name = tempfile.mkstemp(prefix="scan-out-", dir=self.config.work_dir)
        os.close(input_fd)
        os.close(output_fd)
        input_path = Path(input_name)
        output_path = Path(output_name)
        try:
            if command.deadline_at <= datetime.now(UTC):
                return await self._finish(
                    command,
                    status="failed",
                    released_ref=None,
                    error_code="scan_deadline_expired",
                    progress={"attempts": attempts},
                    owner=claim_owner,
                )
            persisted_ref = claimed.progress.get("released_ref")
            if persisted_ref is not None:
                released_ref = ObjectRef.model_validate(persisted_ref)
                try:
                    await self.storage.download_exact(
                        released_ref,
                        destination=output_path,
                        max_bytes=self.config.max_object_bytes,
                    )
                except StorageError:
                    return await self._retry_or_fail(
                        command,
                        attempts=attempts,
                        error_code="released_verification_failed",
                        progress={"attempts": attempts, "released_ref": persisted_ref},
                        owner=claim_owner,
                    )
                return await self._finish(
                    command,
                    status="clean",
                    released_ref=released_ref,
                    error_code=None,
                    progress={"attempts": attempts, "released_ref": persisted_ref},
                    owner=claim_owner,
                )
            try:
                await self.storage.download_exact(
                    command.quarantine_ref,
                    destination=input_path,
                    max_bytes=self.config.max_object_bytes,
                )
            except StorageError:
                return await self._retry_or_fail(
                    command,
                    attempts=attempts,
                    error_code="quarantine_read_failed",
                    progress={"attempts": attempts},
                    owner=claim_owner,
                )
            try:
                result: PolicyResult = await asyncio.wait_for(
                    self.policy.scan(
                        input_path, declared_content_type=command.content_type
                    ),
                    timeout=self.config.policy_timeout_seconds,
                )
            except TimeoutError:
                return await self._retry_or_fail(
                    command,
                    attempts=attempts,
                    error_code="scanner_policy_timeout",
                    progress={"attempts": attempts},
                    owner=claim_owner,
                )
            if result.status == "flagged":
                return await self._finish(
                    command,
                    status="flagged",
                    released_ref=None,
                    error_code=result.error_code,
                    progress={"attempts": attempts},
                    owner=claim_owner,
                )
            if result.status == "failed":
                return await self._retry_or_fail(
                    command,
                    attempts=attempts,
                    error_code=result.error_code or "scanner_failed",
                    progress={"attempts": attempts},
                    owner=claim_owner,
                )
            released_ref = None
            try:
                released_ref = await self.storage.put_file(
                    zone="released",
                    bucket=self.config.released_bucket,
                    key=_released_key(command),
                    source=input_path,
                    content_type=command.content_type,
                    sha256=command.sha256,
                    byte_size=command.byte_size,
                    operation_id=command.scan_job_id,
                )
                progress = {
                    "attempts": attempts,
                    "released_ref": released_ref.model_dump(mode="json"),
                }
                await self.store.set_progress(
                    scan_job_id,
                    state="running",
                    progress=progress,
                    owner=claim_owner,
                )
                await self.storage.download_exact(
                    released_ref,
                    destination=output_path,
                    max_bytes=self.config.max_object_bytes,
                )
            except StorageError:
                return await self._retry_or_fail(
                    command,
                    attempts=attempts,
                    error_code="promotion_failed",
                    progress={
                        "attempts": attempts,
                        "released_ref": (
                            None
                            if released_ref is None
                            else released_ref.model_dump(mode="json")
                        ),
                    },
                    owner=claim_owner,
                )
            return await self._finish(
                command,
                status="clean",
                released_ref=released_ref,
                error_code=None,
                progress=progress,
                owner=claim_owner,
            )
        finally:
            input_path.unlink(missing_ok=True)
            output_path.unlink(missing_ok=True)

    async def run_pending_once(self, *, limit: int = 20) -> int:
        processed = 0
        for scan_job_id in await self.store.list_runnable(
            kind="scan", states=("pending", "running"), limit=limit
        ):
            try:
                if await self.process(scan_job_id) is not None:
                    processed += 1
            except Exception:  # noqa: BLE001 - durable claim expiry enables recovery
                logger.exception("artifact scan job %s failed unexpectedly", scan_job_id)
        return processed

    async def run_worker(self, *, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            await self.run_pending_once()
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.config.poll_seconds)
            except TimeoutError:
                pass
