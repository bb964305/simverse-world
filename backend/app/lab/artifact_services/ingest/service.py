"""One-time bounded upload leases and immutable quarantine ingestion."""
from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator

from app.lab.artifact_services.auth import (
    RequestBinding,
    ServiceTokenValidator,
    UploadCapabilityClaims,
    UploadCapabilityIssuer,
)
from app.lab.artifact_services.canonical import canonical_digest
from app.lab.artifact_services.mime import SNIFF_BYTES, declared_mime_matches, sniff_mime
from app.lab.artifact_services.receipts import ReceiptSigner
from app.lab.artifact_services.schemas import (
    ObjectRef,
    UploadLeaseCommand,
    UploadLeaseReceipt,
    UploadReceipt,
)
from app.lab.artifact_services.storage.base import ObjectStorage, StorageError
from app.lab.artifact_services.store import OperationConflict, OperationStore


class IngestError(RuntimeError):
    status_code = 400

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class IngestBusy(IngestError):
    status_code = 409


class IngestNotFound(IngestError):
    status_code = 404


class IngestUnavailable(IngestError):
    status_code = 503


@dataclass(frozen=True)
class IngestConfig:
    service_instance_id: str
    quarantine_bucket: str
    spool_dir: Path
    max_upload_bytes: int
    max_lease_ttl_seconds: int = 300
    upload_claim_seconds: int = 900

    def __post_init__(self) -> None:
        if not self.service_instance_id or not self.quarantine_bucket:
            raise ValueError("ingest instance ID and quarantine bucket are required")
        if self.max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")
        if not 1 <= self.max_lease_ttl_seconds <= 900:
            raise ValueError("upload lease TTL must be between 1 and 900 seconds")
        if self.upload_claim_seconds < self.max_lease_ttl_seconds:
            raise ValueError("upload claim must cover the complete lease lifetime")


def _stable_receipt_id(kind: str, operation_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"simverse:{kind}:{operation_id}"))


def _object_key(command: UploadLeaseCommand, sha256: str) -> str:
    def segment(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]

    return "/".join(
        [
            segment(command.tenant_id),
            segment(command.run_id),
            segment(command.artifact_id),
            segment(command.upload_id),
            sha256,
        ]
    )


class IngestService:
    def __init__(
        self,
        *,
        config: IngestConfig,
        store: OperationStore,
        storage: ObjectStorage,
        receipt_signer: ReceiptSigner,
        upload_issuer: UploadCapabilityIssuer,
        upload_validator: ServiceTokenValidator,
    ) -> None:
        self.config = config
        self.store = store
        self.storage = storage
        self.receipt_signer = receipt_signer
        self.upload_issuer = upload_issuer
        self.upload_validator = upload_validator

    async def initialize(self) -> None:
        self.config.spool_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.config.spool_dir, 0o700)
        await self.store.initialize()

    async def ready(self) -> bool:
        try:
            if not await self.store.ready() or not await self.storage.ready():
                return False
            fd, path = tempfile.mkstemp(prefix=".ready-", dir=self.config.spool_dir)
            os.close(fd)
            Path(path).unlink()
            return True
        except Exception:
            return False

    async def create_upload_lease(
        self, command: UploadLeaseCommand, *, now: datetime | None = None
    ) -> UploadLeaseReceipt:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        remaining = (command.expires_at - current).total_seconds()
        if remaining <= 0 or remaining > self.config.max_lease_ttl_seconds:
            raise IngestError("invalid_lease_expiry")
        if command.max_bytes > self.config.max_upload_bytes:
            raise IngestError("upload_limit_exceeds_service_cap")
        digest = canonical_digest(command)
        try:
            claim = await self.store.create_or_get(
                operation_id=command.upload_id,
                kind="upload",
                command_digest=digest,
                command=command.model_dump(mode="json"),
                initial_state="leased",
            )
        except OperationConflict as exc:
            raise IngestBusy("upload_id_conflict") from exc
        if claim.record.response is not None:
            if claim.record.response.get("receipt_type") == "artifact.upload_lease":
                return UploadLeaseReceipt.model_validate(claim.record.response)
            raise IngestBusy("upload_already_consumed")

        issued_at = int(current.timestamp())
        upload_token = self.upload_issuer.issue(command, now=issued_at)
        outcome = {
            "upload_id": command.upload_id,
            "expires_at": command.expires_at.isoformat(),
            "max_bytes": command.max_bytes,
        }
        receipt = self.receipt_signer.sign(
            UploadLeaseReceipt,
            {
                "receipt_type": "artifact.upload_lease",
                "receipt_id": _stable_receipt_id("upload-lease", command.upload_id),
                "service_instance_id": self.config.service_instance_id,
                "command_id": command.command_id,
                "action": "artifact.lease.create",
                "request_digest": digest,
                "tenant_id": command.tenant_id,
                "run_id": command.run_id,
                "session_id": command.session_id,
                "artifact_id": command.artifact_id,
                "producer_action_id": command.producer_action_id,
                "epoch": command.epoch,
                "status": "leased",
                "occurred_at": datetime.fromtimestamp(issued_at, tz=UTC),
                "payload_digest": canonical_digest(outcome),
                "upload_id": command.upload_id,
                "expires_at": command.expires_at,
                "max_bytes": command.max_bytes,
                "upload_token": upload_token,
            },
        )
        await self.store.set_response(
            command.upload_id,
            state="leased",
            response=receipt.model_dump(mode="json"),
            expected_states=("leased",),
        )
        return receipt

    def validate_upload_token(
        self,
        token: str,
        *,
        command: UploadLeaseCommand,
        allow_expired: bool = False,
    ) -> UploadCapabilityClaims:
        binding = RequestBinding.from_command(command, operation_id=command.upload_id)
        claims = self.upload_validator.validate(
            token,
            action="artifact.upload",
            binding=binding,
            claims_type=UploadCapabilityClaims,
            allow_expired=allow_expired,
        )
        assert isinstance(claims, UploadCapabilityClaims)
        if (
            claims.command_digest != canonical_digest(command)
            or claims.max_bytes != command.max_bytes
            or claims.content_type != command.content_type
            or claims.expected_sha256 != command.expected_sha256
            or claims.declared_byte_size != command.declared_byte_size
        ):
            raise IngestError("upload_capability_binding_mismatch")
        return claims

    async def get_upload_command(self, upload_id: str) -> UploadLeaseCommand:
        existing = await self.store.get(upload_id)
        if existing is None or existing.kind != "upload":
            raise IngestNotFound("upload_not_found")
        command = UploadLeaseCommand.model_validate(existing.command)
        if command.upload_id != upload_id:
            raise IngestError("upload_path_binding_mismatch")
        return command

    async def get_upload_receipt(self, upload_id: str) -> UploadReceipt:
        existing = await self.store.get(upload_id)
        if existing is None or existing.kind != "upload":
            raise IngestNotFound("upload_not_found")
        if existing.response and existing.response.get("receipt_type") == "artifact.upload":
            return UploadReceipt.model_validate(existing.response)
        command = UploadLeaseCommand.model_validate(existing.command)
        if command.expires_at > datetime.now(UTC):
            raise IngestBusy("upload_pending")
        owner_id = (
            f"recovery:{self.config.service_instance_id}:{uuid.uuid4()}"
        )
        claimed = await self.store.claim(
            upload_id,
            owner=owner_id,
            eligible_states=("leased", "uploading"),
            claimed_state="uploading",
            lease_seconds=self.config.upload_claim_seconds,
        )
        if claimed is None:
            refreshed = await self.store.get(upload_id)
            if (
                refreshed
                and refreshed.response
                and refreshed.response.get("receipt_type") == "artifact.upload"
            ):
                return UploadReceipt.model_validate(refreshed.response)
            raise IngestBusy("upload_recovery_in_progress")
        request_digest = canonical_digest(command)
        stored_ref = claimed.progress.get("quarantine_ref")
        if stored_ref is None:
            return await self._failed_receipt(
                command=command,
                request_digest=request_digest,
                error_code="upload_lease_expired",
                owner_id=owner_id,
            )
        try:
            quarantine_ref = ObjectRef.model_validate(stored_ref)
        except Exception as exc:  # noqa: BLE001 - durable state is untrusted input
            raise IngestUnavailable("upload_recovery_locator_invalid") from exc
        mismatch = self._upload_mismatch(command, quarantine_ref)
        if mismatch:
            return await self._failed_receipt(
                command=command,
                request_digest=request_digest,
                error_code=mismatch,
                quarantine_ref=quarantine_ref,
                byte_size=quarantine_ref.byte_size,
                sha256=quarantine_ref.sha256,
                content_type=quarantine_ref.content_type,
                owner_id=owner_id,
            )
        return await self._completed_receipt(
            command=command,
            request_digest=request_digest,
            quarantine_ref=quarantine_ref,
            owner_id=owner_id,
        )

    @staticmethod
    def _upload_mismatch(
        command: UploadLeaseCommand, quarantine_ref: ObjectRef
    ) -> str | None:
        if (
            command.expected_sha256
            and command.expected_sha256 != quarantine_ref.sha256
        ):
            return "sha256_mismatch"
        if (
            command.declared_byte_size is not None
            and command.declared_byte_size != quarantine_ref.byte_size
        ):
            return "byte_size_mismatch"
        if not declared_mime_matches(
            command.content_type, quarantine_ref.content_type
        ):
            return "content_type_mismatch"
        return None

    async def _completed_receipt(
        self,
        *,
        command: UploadLeaseCommand,
        request_digest: str,
        quarantine_ref: ObjectRef,
        owner_id: str,
    ) -> UploadReceipt:
        occurred = datetime.now(UTC)
        outcome = {
            "status": "completed",
            "quarantine_ref": quarantine_ref.model_dump(mode="json"),
            "byte_size": quarantine_ref.byte_size,
            "sha256": quarantine_ref.sha256,
            "content_type": quarantine_ref.content_type,
        }
        receipt = self.receipt_signer.sign(
            UploadReceipt,
            {
                "receipt_type": "artifact.upload",
                "receipt_id": _stable_receipt_id("upload", command.upload_id),
                "service_instance_id": self.config.service_instance_id,
                "command_id": command.command_id,
                "action": "artifact.upload",
                "request_digest": request_digest,
                "tenant_id": command.tenant_id,
                "run_id": command.run_id,
                "session_id": command.session_id,
                "artifact_id": command.artifact_id,
                "producer_action_id": command.producer_action_id,
                "epoch": command.epoch,
                "status": "completed",
                "occurred_at": occurred,
                "payload_digest": canonical_digest(outcome),
                "upload_id": command.upload_id,
                "quarantine_ref": quarantine_ref,
                "byte_size": quarantine_ref.byte_size,
                "sha256": quarantine_ref.sha256,
                "content_type": quarantine_ref.content_type,
                "completed_at": occurred,
                "error_code": None,
            },
        )
        await self.store.set_response(
            command.upload_id,
            state="completed",
            response=receipt.model_dump(mode="json"),
            progress={
                "quarantine_ref": quarantine_ref.model_dump(mode="json")
            },
            expected_states=("uploading",),
            owner=owner_id,
        )
        return receipt

    async def _failed_receipt(
        self,
        *,
        command: UploadLeaseCommand,
        request_digest: str,
        error_code: str,
        quarantine_ref=None,
        byte_size: int | None = None,
        sha256: str | None = None,
        content_type: str | None = None,
        owner_id: str,
    ) -> UploadReceipt:
        occurred = datetime.now(UTC)
        outcome = {
            "status": "failed",
            "error_code": error_code,
            "quarantine_ref": (
                None if quarantine_ref is None else quarantine_ref.model_dump(mode="json")
            ),
            "byte_size": byte_size,
            "sha256": sha256,
            "content_type": content_type,
        }
        receipt = self.receipt_signer.sign(
            UploadReceipt,
            {
                "receipt_type": "artifact.upload",
                "receipt_id": _stable_receipt_id("upload", command.upload_id),
                "service_instance_id": self.config.service_instance_id,
                "command_id": command.command_id,
                "action": "artifact.upload",
                "request_digest": request_digest,
                "tenant_id": command.tenant_id,
                "run_id": command.run_id,
                "session_id": command.session_id,
                "artifact_id": command.artifact_id,
                "producer_action_id": command.producer_action_id,
                "epoch": command.epoch,
                "status": "failed",
                "occurred_at": occurred,
                "payload_digest": canonical_digest(outcome),
                "upload_id": command.upload_id,
                "quarantine_ref": quarantine_ref,
                "byte_size": byte_size,
                "sha256": sha256,
                "content_type": content_type,
                "completed_at": occurred,
                "error_code": error_code,
            },
        )
        await self.store.set_response(
            command.upload_id,
            state="failed",
            response=receipt.model_dump(mode="json"),
            progress={"quarantine_ref": outcome["quarantine_ref"]},
            error_code=error_code,
            expected_states=("uploading",),
            owner=owner_id,
        )
        return receipt

    async def upload(
        self,
        *,
        upload_id: str,
        chunks: AsyncIterator[bytes],
        token: str,
        owner_id: str,
    ) -> UploadReceipt:
        existing = await self.store.get(upload_id)
        if existing is None or existing.kind != "upload":
            raise IngestNotFound("upload_not_found")
        command = UploadLeaseCommand.model_validate(existing.command)
        if command.upload_id != upload_id:
            raise IngestError("upload_path_binding_mismatch")
        if existing.response and existing.response.get("receipt_type") == "artifact.upload":
            self.validate_upload_token(
                token, command=command, allow_expired=True
            )
            return UploadReceipt.model_validate(existing.response)
        self.validate_upload_token(token, command=command)
        claimed = await self.store.claim(
            upload_id,
            owner=owner_id,
            eligible_states=("leased", "uploading"),
            claimed_state="uploading",
            lease_seconds=self.config.upload_claim_seconds,
        )
        if claimed is None:
            refreshed = await self.store.get(upload_id)
            if (
                refreshed
                and refreshed.response
                and refreshed.response.get("receipt_type") == "artifact.upload"
            ):
                return UploadReceipt.model_validate(refreshed.response)
            raise IngestBusy("upload_in_progress")

        fd, temp_name = tempfile.mkstemp(prefix="upload-", dir=self.config.spool_dir)
        temp_path = Path(temp_name)
        digest = hashlib.sha256()
        byte_size = 0
        prefix = bytearray()
        request_digest = canonical_digest(command)
        try:
            with os.fdopen(fd, "wb") as output:
                async for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise IngestError("upload_chunk_not_bytes")
                    byte_size += len(chunk)
                    if byte_size > command.max_bytes or byte_size > self.config.max_upload_bytes:
                        return await self._failed_receipt(
                            command=command,
                            request_digest=request_digest,
                            error_code="upload_too_large",
                            owner_id=owner_id,
                        )
                    digest.update(chunk)
                    if len(prefix) < SNIFF_BYTES:
                        prefix.extend(chunk[: SNIFF_BYTES - len(prefix)])
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())

            actual_sha = digest.hexdigest()
            actual_mime = sniff_mime(bytes(prefix))
            key = _object_key(command, actual_sha)
            try:
                quarantine_ref = await self.storage.put_file(
                    zone="quarantine",
                    bucket=self.config.quarantine_bucket,
                    key=key,
                    source=temp_path,
                    content_type=actual_mime,
                    sha256=actual_sha,
                    byte_size=byte_size,
                    operation_id=upload_id,
                )
            except StorageError as exc:
                await self.store.set_progress(
                    upload_id,
                    state="leased",
                    progress={"error": "storage_unavailable"},
                    error_code="storage_unavailable",
                    owner=owner_id,
                    clear_claim=True,
                )
                raise IngestUnavailable("storage_unavailable") from exc
            await self.store.set_progress(
                upload_id,
                state="uploading",
                progress={
                    "quarantine_ref": quarantine_ref.model_dump(mode="json")
                },
                owner=owner_id,
            )

            mismatch = self._upload_mismatch(command, quarantine_ref)
            if mismatch:
                return await self._failed_receipt(
                    command=command,
                    request_digest=request_digest,
                    error_code=mismatch,
                    quarantine_ref=quarantine_ref,
                    byte_size=byte_size,
                    sha256=actual_sha,
                    content_type=actual_mime,
                    owner_id=owner_id,
                )

            return await self._completed_receipt(
                command=command,
                request_digest=request_digest,
                quarantine_ref=quarantine_ref,
                owner_id=owner_id,
            )
        finally:
            temp_path.unlink(missing_ok=True)
