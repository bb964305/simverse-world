"""Recoverable Executor-to-Ingest upload using one bounded output capability."""
from __future__ import annotations

import asyncio
import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx
from pydantic import ValidationError

from app.lab.artifact_services.mime import declared_mime_matches
from app.lab.artifact_services.schemas import UploadReceipt
from app.lab.protocol import ExecutorOutputSpec
from app.lab.runtime_ref.service_auth import MAX_REQUEST_BYTES

from .store import StoredArtifactUpload


_READ_CHUNK = 64 * 1024


class ExecutorArtifactUploadError(RuntimeError):
    def __init__(self, error_code: str, *, uncertain: bool) -> None:
        self.error_code = error_code
        self.uncertain = uncertain
        super().__init__(error_code)


@dataclass(frozen=True)
class ExecutorArtifactUploaderConfig:
    ingest_base_url: str
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.ingest_base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Executor Ingest base URL must be an HTTP(S) origin")
        if self.timeout_seconds <= 0:
            raise ValueError("Executor artifact upload timeout must be positive")


class ExecutorArtifactUploader:
    def __init__(self, config: ExecutorArtifactUploaderConfig) -> None:
        self.config = config
        self._base = urlsplit(config.ingest_base_url.rstrip("/"))

    def validate_spec(self, spec: ExecutorOutputSpec) -> str:
        lease = spec.lease
        if quote(lease.upload_id, safe="") != lease.upload_id:
            raise ExecutorArtifactUploadError(
                "artifact_upload_id_invalid", uncertain=False
            )
        target = urlsplit(lease.upload_url)
        base_path = self._base.path.rstrip("/")
        expected_path = f"{base_path}/v1/uploads/{lease.upload_id}"
        if (
            target.scheme != self._base.scheme
            or target.netloc != self._base.netloc
            or target.path != expected_path
            or target.query
            or target.fragment
            or target.username is not None
            or target.password is not None
        ):
            raise ExecutorArtifactUploadError(
                "artifact_upload_url_not_allowed", uncertain=False
            )
        return lease.upload_url

    @staticmethod
    async def _verify_file(path: Path, upload: StoredArtifactUpload) -> None:
        def verify() -> None:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, flags)
            except OSError as exc:
                raise ExecutorArtifactUploadError(
                    "artifact_spool_unreadable", uncertain=False
                ) from exc
            digest = hashlib.sha256()
            observed = 0
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                    raise ExecutorArtifactUploadError(
                        "artifact_spool_not_regular", uncertain=False
                    )
                with os.fdopen(descriptor, "rb", closefd=False) as handle:
                    while chunk := handle.read(_READ_CHUNK):
                        observed += len(chunk)
                        if observed > upload.spec.max_bytes:
                            raise ExecutorArtifactUploadError(
                                "artifact_spool_too_large", uncertain=False
                            )
                        digest.update(chunk)
                final = os.fstat(descriptor)
                if (
                    final.st_size != opened.st_size
                    or observed != upload.byte_size
                    or digest.hexdigest() != upload.sha256
                ):
                    raise ExecutorArtifactUploadError(
                        "artifact_spool_changed", uncertain=False
                    )
            finally:
                os.close(descriptor)

        await asyncio.to_thread(verify)

    @staticmethod
    async def _file_chunks(path: Path, upload: StoredArtifactUpload):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = await asyncio.to_thread(os.open, path, flags)
        except OSError as exc:
            raise ExecutorArtifactUploadError(
                "artifact_spool_unreadable", uncertain=False
            ) from exc
        digest = hashlib.sha256()
        observed = 0
        try:
            opened = await asyncio.to_thread(os.fstat, descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size != upload.byte_size
            ):
                raise ExecutorArtifactUploadError(
                    "artifact_spool_changed", uncertain=False
                )
            while True:
                chunk = await asyncio.to_thread(os.read, descriptor, _READ_CHUNK)
                if not chunk:
                    break
                observed += len(chunk)
                if observed > upload.spec.max_bytes:
                    raise ExecutorArtifactUploadError(
                        "artifact_spool_too_large", uncertain=True
                    )
                digest.update(chunk)
                yield chunk
            final = await asyncio.to_thread(os.fstat, descriptor)
            if (
                final.st_size != opened.st_size
                or observed != upload.byte_size
                or digest.hexdigest() != upload.sha256
            ):
                raise ExecutorArtifactUploadError(
                    "artifact_spool_changed", uncertain=True
                )
        finally:
            await asyncio.to_thread(os.close, descriptor)

    @staticmethod
    def _validate_receipt(
        receipt: UploadReceipt, upload: StoredArtifactUpload
    ) -> None:
        spec = upload.spec
        lease = spec.lease
        if (
            receipt.upload_id != lease.upload_id
            or receipt.action != "artifact.upload"
            or receipt.tenant_id != lease.tenant_id
            or receipt.run_id != lease.run_id
            or receipt.session_id != lease.session_id
            or receipt.artifact_id != spec.artifact_id
            or receipt.producer_action_id != lease.producer_action_id
            or receipt.epoch != lease.epoch
        ):
            raise ExecutorArtifactUploadError(
                "artifact_receipt_binding_mismatch", uncertain=False
            )
        if receipt.status == "completed" and (
            receipt.byte_size != upload.byte_size
            or receipt.sha256 != upload.sha256
            or not declared_mime_matches(
                spec.content_type, receipt.content_type or ""
            )
        ):
            raise ExecutorArtifactUploadError(
                "artifact_receipt_bytes_mismatch", uncertain=False
            )

    async def upload(
        self, upload: StoredArtifactUpload, *, path: Path
    ) -> UploadReceipt:
        target = self.validate_spec(upload.spec)
        await self._verify_file(path, upload)
        lease = upload.spec.lease
        headers = {
            "Authorization": f"Bearer {lease.bearer_token}",
            "Content-Type": upload.spec.content_type,
            "Content-Length": str(upload.byte_size),
            "X-Artifact-Upload-Id": lease.upload_id,
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(self.config.timeout_seconds),
            ) as client:
                async with client.stream(
                    "PUT",
                    target,
                    headers=headers,
                    content=self._file_chunks(path, upload),
                ) as response:
                    status_code = response.status_code
                    encoded = bytearray()
                    async for chunk in response.aiter_bytes():
                        encoded.extend(chunk)
                        if len(encoded) > MAX_REQUEST_BYTES:
                            raise ExecutorArtifactUploadError(
                                "artifact_receipt_too_large", uncertain=False
                            )
        except ExecutorArtifactUploadError:
            raise
        except (httpx.TimeoutException, httpx.TransportError, OSError) as exc:
            raise ExecutorArtifactUploadError(
                "artifact_upload_transport_uncertain", uncertain=True
            ) from exc

        if status_code == 409 or status_code in {408, 429} or status_code >= 500:
            raise ExecutorArtifactUploadError(
                "artifact_upload_outcome_uncertain", uncertain=True
            )
        if status_code not in {201, 422}:
            raise ExecutorArtifactUploadError(
                "artifact_upload_rejected", uncertain=False
            )
        try:
            receipt = UploadReceipt.model_validate_json(bytes(encoded), strict=True)
        except (ValidationError, ValueError, UnicodeError) as exc:
            raise ExecutorArtifactUploadError(
                "artifact_receipt_invalid", uncertain=False
            ) from exc
        self._validate_receipt(receipt, upload)
        if (status_code == 201) != (receipt.status == "completed"):
            raise ExecutorArtifactUploadError(
                "artifact_receipt_status_mismatch", uncertain=False
            )
        return receipt


def resolve_spool_path(root: Path, relative_path: str) -> Path:
    """Resolve a persisted locator without following any mutable symlink."""
    parts = relative_path.split("/")
    if (
        relative_path.startswith("/")
        or "\\" in relative_path
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ExecutorArtifactUploadError(
            "artifact_spool_locator_invalid", uncertain=False
        )
    current = root
    for index, part in enumerate(parts):
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError as exc:
            raise ExecutorArtifactUploadError(
                "artifact_spool_missing", uncertain=False
            ) from exc
        if stat.S_ISLNK(current_stat.st_mode):
            raise ExecutorArtifactUploadError(
                "artifact_spool_symlink", uncertain=False
            )
        if index < len(parts) - 1 and not stat.S_ISDIR(current_stat.st_mode):
            raise ExecutorArtifactUploadError(
                "artifact_spool_parent_invalid", uncertain=False
            )
    if not stat.S_ISREG(current_stat.st_mode):
        raise ExecutorArtifactUploadError(
            "artifact_spool_not_regular", uncertain=False
        )
    return current
