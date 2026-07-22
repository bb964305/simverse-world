"""Recoverable Runtime-to-Ingest artifact upload coordinator."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from app.lab.artifact_services.mime import declared_mime_matches
from app.lab.artifact_services.schemas import UploadReceipt
from app.lab.protocol import RuntimeArtifactUploadCommand
from app.lab.runtime_ref.service_auth import MAX_REQUEST_BYTES, canonical_json_bytes
from app.lab.runtime_ref.spool import ArtifactSpoolError
from app.lab.runtime_ref.store import (
    RuntimeStore,
    RuntimeStoreConflict,
    RuntimeStoreNotFound,
    StoredArtifact,
)


class ArtifactUploadError(RuntimeError):
    def __init__(self, error_code: str, *, retryable: bool) -> None:
        self.error_code = error_code
        self.retryable = retryable
        super().__init__(error_code)


class ArtifactUploader:
    def __init__(
        self,
        *,
        store: RuntimeStore,
        ingest_base_url: str,
        timeout_seconds: float,
    ) -> None:
        self.store = store
        self.ingest_base_url = ingest_base_url.rstrip("/")
        if not self.ingest_base_url:
            raise ValueError("artifact ingest base URL is required")
        if timeout_seconds <= 0:
            raise ValueError("artifact upload timeout must be positive")
        self.timeout_seconds = timeout_seconds
        self._base = self._validated_url(self.ingest_base_url)
        self._artifact_locks: dict[tuple[str, str], asyncio.Lock] = {}

    @staticmethod
    def _validated_url(value: str):
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("artifact ingest URL must be an HTTP(S) service URL")
        return parsed

    def _validate_upload_url(self, value: str) -> None:
        target = self._validated_url(value)
        base_path = self._base.path.rstrip("/")
        if (
            target.scheme != self._base.scheme
            or target.netloc != self._base.netloc
            or not (
                target.path == base_path
                or target.path.startswith(f"{base_path}/")
            )
        ):
            raise ArtifactUploadError("upload_url_not_allowed", retryable=False)

    def _lock(self, session_id: str, artifact_id: str) -> asyncio.Lock:
        return self._artifact_locks.setdefault(
            (session_id, artifact_id), asyncio.Lock()
        )

    @staticmethod
    def _command_from_stored(value: Any) -> RuntimeArtifactUploadCommand:
        try:
            return RuntimeArtifactUploadCommand.model_validate_json(
                canonical_json_bytes(value)
            )
        except (ValidationError, ValueError, TypeError) as exc:
            raise ArtifactUploadError(
                "stored_upload_command_invalid", retryable=False
            ) from exc

    @staticmethod
    def _validate_binding(
        command: RuntimeArtifactUploadCommand, artifact: StoredArtifact
    ) -> None:
        lease = command.lease
        if (
            command.session_id != artifact.session_id
            or command.provider_artifact_id != artifact.artifact_id
            or lease.session_id != artifact.session_id
            or lease.content_type != artifact.content_type
            or lease.expected_sha256 != artifact.expected_sha256
            or lease.producer_action_id != artifact.producer_action_id
            or artifact.spool_locator is None
            or artifact.declared_byte_size is None
            or artifact.expected_sha256 is None
            or artifact.declared_byte_size > lease.max_bytes
        ):
            raise ArtifactUploadError("upload_binding_mismatch", retryable=False)
        if lease.expires_at <= datetime.now(UTC):
            raise ArtifactUploadError("upload_lease_expired", retryable=False)

    @staticmethod
    def _validate_receipt(
        receipt: UploadReceipt,
        *,
        command: RuntimeArtifactUploadCommand,
        artifact: StoredArtifact,
    ) -> None:
        lease = command.lease
        if (
            receipt.upload_id != lease.upload_id
            or receipt.tenant_id != lease.tenant_id
            or receipt.run_id != command.run_id
            or receipt.session_id != command.session_id
            or receipt.artifact_id != lease.artifact_id
            or receipt.producer_action_id != lease.producer_action_id
            or receipt.epoch != command.epoch
        ):
            raise ArtifactUploadError("upload_receipt_binding_mismatch", retryable=False)
        if receipt.status == "completed" and (
            receipt.byte_size != artifact.declared_byte_size
            or receipt.sha256 != artifact.expected_sha256
            or not declared_mime_matches(
                artifact.content_type, receipt.content_type or ""
            )
        ):
            raise ArtifactUploadError("upload_receipt_binding_mismatch", retryable=False)

    async def _send(
        self,
        command: RuntimeArtifactUploadCommand,
        artifact: StoredArtifact,
    ) -> UploadReceipt:
        lease = command.lease
        self._validate_upload_url(lease.upload_url)
        headers = {
            "Authorization": f"Bearer {lease.bearer_token}",
            "Content-Type": artifact.content_type,
            "Content-Length": str(artifact.declared_byte_size),
            "X-Artifact-Upload-Id": lease.upload_id,
        }
        try:
            async with httpx.AsyncClient(
                trust_env=False,
                timeout=httpx.Timeout(self.timeout_seconds),
            ) as client:
                async with client.stream(
                    "PUT",
                    lease.upload_url,
                    headers=headers,
                    content=self.store.artifact_spool.iter_bytes(
                        artifact.spool_locator
                    ),
                ) as response:
                    status_code = response.status_code
                    encoded = bytearray()
                    async for chunk in response.aiter_bytes():
                        encoded.extend(chunk)
                        if len(encoded) > MAX_REQUEST_BYTES:
                            raise ArtifactUploadError(
                                "upload_receipt_too_large", retryable=False
                            )
        except ArtifactUploadError:
            raise
        except (httpx.TransportError, ArtifactSpoolError, OSError) as exc:
            raise ArtifactUploadError("upload_transport_failed", retryable=True) from exc

        if not (200 <= status_code < 300) and status_code != 422:
            retryable = status_code in {408, 429} or status_code >= 500
            raise ArtifactUploadError(
                "upload_ingest_unavailable" if retryable else "upload_ingest_rejected",
                retryable=retryable,
            )
        try:
            receipt = UploadReceipt.model_validate_json(bytes(encoded), strict=True)
        except (ValidationError, ValueError, UnicodeError) as exc:
            raise ArtifactUploadError("upload_receipt_invalid", retryable=False) from exc
        self._validate_receipt(receipt, command=command, artifact=artifact)
        if (200 <= status_code < 300) != (receipt.status == "completed"):
            raise ArtifactUploadError("upload_receipt_status_mismatch", retryable=False)
        return receipt

    async def execute(
        self,
        command: RuntimeArtifactUploadCommand,
        *,
        command_digest: str,
    ) -> StoredArtifact:
        async with self._lock(command.session_id, command.provider_artifact_id):
            artifact = await self.store.get_artifact(
                command.session_id, command.provider_artifact_id
            )
            if artifact is None:
                raise RuntimeStoreNotFound("artifact not found")
            self._validate_binding(command, artifact)
            if artifact.spool_locator is None:
                raise ArtifactUploadError("artifact_spool_missing", retryable=False)
            try:
                spooled = await self.store.artifact_spool.digest(
                    artifact.spool_locator
                )
            except (ArtifactSpoolError, OSError) as exc:
                raise ArtifactUploadError("artifact_spool_missing", retryable=False) from exc
            if (
                spooled.byte_size != artifact.declared_byte_size
                or spooled.sha256 != artifact.expected_sha256
            ):
                raise ArtifactUploadError("artifact_spool_digest_mismatch", retryable=False)

            artifact = await self.store.claim_artifact_upload(
                command.session_id,
                command.provider_artifact_id,
                upload_id=command.lease.upload_id,
                command=command.model_dump(mode="json"),
                command_digest=command_digest,
            )
            if artifact.upload_receipt_digest is not None:
                return artifact
            try:
                receipt = await self._send(command, artifact)
                receipt_json = receipt.model_dump(mode="json")
                receipt_digest = hashlib.sha256(
                    canonical_json_bytes(receipt_json)
                ).hexdigest()
                return await self.store.record_artifact_upload(
                    command.session_id,
                    command.provider_artifact_id,
                    upload_id=command.lease.upload_id,
                    command_digest=command_digest,
                    receipt=receipt_json,
                    receipt_digest=receipt_digest,
                    succeeded=receipt.status == "completed",
                    error_code=receipt.error_code,
                )
            except ArtifactUploadError as exc:
                await self.store.record_artifact_upload_failure(
                    command.session_id,
                    command.provider_artifact_id,
                    error_code=exc.error_code,
                )
                raise

    async def recover_once(self) -> None:
        for artifact in await self.store.list_recoverable_artifact_uploads():
            if artifact.upload_command is None or artifact.upload_command_digest is None:
                continue
            try:
                command = self._command_from_stored(artifact.upload_command)
                await self.execute(
                    command, command_digest=artifact.upload_command_digest
                )
            except ArtifactUploadError as exc:
                try:
                    await self.store.record_artifact_upload_failure(
                        artifact.session_id,
                        artifact.artifact_id,
                        error_code=exc.error_code,
                    )
                except (RuntimeStoreConflict, RuntimeStoreNotFound, ValueError):
                    pass
                continue
            except (
                RuntimeStoreConflict,
                RuntimeStoreNotFound,
                ValueError,
            ):
                continue

    async def cleanup_acknowledged_spools(self) -> None:
        for artifact in await self.store.list_acked_artifact_spools():
            if artifact.spool_locator is None:
                continue
            try:
                await self.store.artifact_spool.delete(artifact.spool_locator)
                await self.store.mark_artifact_spool_deleted(
                    artifact.session_id, artifact.artifact_id
                )
            except (ArtifactSpoolError, RuntimeStoreConflict, RuntimeStoreNotFound, OSError):
                continue

    async def recovery_loop(
        self, stop: asyncio.Event, *, interval_seconds: float
    ) -> None:
        while not stop.is_set():
            await self.cleanup_acknowledged_spools()
            await self.recover_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                continue
