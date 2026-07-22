"""Gateway-side controller for the isolated production Artifact services."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

import httpx
from sqlalchemy import select

from app.config import settings
from app.lab.artifact_services.auth import (
    JwtIssuerConfig,
    RequestBinding,
    ServiceTokenIssuer,
)
from app.lab.artifact_services.canonical import canonical_digest
from app.lab.artifact_services.receipts import (
    Ed25519ReceiptVerifier,
    HmacReceiptVerifier,
    ReceiptVerifier,
    ReceiptSignatureError,
)
from app.lab.artifact_services.schemas import (
    DeleteCommand,
    DeleteReceipt,
    DeleteTarget,
    ObjectRef,
    ScanCommand,
    ScanReceipt,
    UploadLeaseCommand,
    UploadLeaseReceipt,
    UploadReceipt,
)
from app.lab.protocol import ArtifactUploadLease
from app.models.lab_artifact import (
    LabArtifact,
    LabArtifactHold,
    LabArtifactOperation,
)
from app.models.lab_event import OutboxEvent


logger = logging.getLogger(__name__)


class ArtifactPipelineError(RuntimeError):
    pass


class ArtifactPipelineConfigurationError(ArtifactPipelineError):
    pass


class ArtifactReceiptError(ArtifactPipelineError):
    pass


class ArtifactOperationNotFound(ArtifactPipelineError):
    pass


class ArtifactOperationPending(ArtifactPipelineError):
    pass


_RECEIPT_ROLES = {
    "artifact.upload_lease": "ingest",
    "artifact.upload": "ingest",
    "artifact.scan": "scanner",
    "artifact.delete": "cleanup",
}


def _expected_receipt_issuers() -> dict[str, str]:
    issuers = {
        "ingest": settings.lab_artifact_ingest_receipt_issuer,
        "scanner": settings.lab_artifact_scanner_receipt_issuer,
        "cleanup": settings.lab_artifact_cleanup_receipt_issuer,
    }
    if any(
        not value
        or value != value.strip()
        or any(ord(char) < 32 for char in value)
        for value in issuers.values()
    ):
        raise ArtifactPipelineConfigurationError(
            "artifact receipt issuers must be non-empty canonical values"
        )
    if len(set(issuers.values())) != len(issuers):
        raise ArtifactPipelineConfigurationError(
            "artifact receipt issuers must be distinct per service role"
        )
    return issuers


def _load_receipt_trust() -> tuple[ReceiptVerifier, dict[str, str], set[str]]:
    expected_issuers = _expected_receipt_issuers()
    try:
        raw_trust = json.loads(settings.lab_artifact_receipt_keys_json)
    except json.JSONDecodeError as exc:
        raise ArtifactPipelineConfigurationError(
            "LAB_ARTIFACT_RECEIPT_KEYS_JSON must be valid JSON"
        ) from exc
    algorithm = settings.lab_artifact_receipt_algorithm
    if algorithm not in {"EdDSA", "HS256"}:
        raise ArtifactPipelineConfigurationError(
            "LAB_ARTIFACT_RECEIPT_ALGORITHM must be EdDSA or HS256"
        )
    if (
        not isinstance(raw_trust, dict)
        or set(raw_trust) != set(expected_issuers.values())
        or any(
            not isinstance(keys, dict)
            or len(keys) < 2
            or any(
                not isinstance(kid, str)
                or not kid
                or kid != kid.strip()
                or not isinstance(key, str)
                or not key
                for kid, key in keys.items()
            )
            or len(set(keys.values())) != len(keys)
            for keys in raw_trust.values()
        )
    ):
        raise ArtifactPipelineConfigurationError(
            "artifact receipt trust must exactly map each service issuer to distinct current/next keys"
        )
    if algorithm == "HS256" and any(
        len(key.encode("utf-8")) < 32
        for keys in raw_trust.values()
        for key in keys.values()
    ):
        raise ArtifactPipelineConfigurationError(
            "artifact HMAC receipt keys must be at least 32 bytes"
        )
    if algorithm == "HS256" and settings.lab_global_admission_enabled:
        raise ArtifactPipelineConfigurationError(
            "global admission requires asymmetric EdDSA receipt verification"
        )
    receipt_keys = {
        key for keys in raw_trust.values() for key in keys.values()
    }
    if len(receipt_keys) != sum(len(keys) for keys in raw_trust.values()):
        raise ArtifactPipelineConfigurationError(
            "artifact receipt keys must not be reused across service roles"
        )
    verifier: ReceiptVerifier
    try:
        verifier = (
            Ed25519ReceiptVerifier(issuers=raw_trust)
            if algorithm == "EdDSA"
            else HmacReceiptVerifier(issuers=raw_trust)
        )
    except ValueError as exc:
        raise ArtifactPipelineConfigurationError(str(exc)) from exc
    return (
        verifier,
        expected_issuers,
        receipt_keys,
    )


def _verify_receipt_binding(
    receipt,
    *,
    operation: LabArtifactOperation,
    verifier: ReceiptVerifier,
    expected_issuers: Mapping[str, str],
):
    role = _RECEIPT_ROLES.get(receipt.receipt_type)
    expected_action = {
        "artifact.upload_lease": "artifact.lease.create",
        "artifact.upload": "artifact.upload",
        "artifact.scan": "artifact.scan",
        "artifact.delete": "artifact.delete",
    }.get(receipt.receipt_type)
    if role is None or receipt.issuer != expected_issuers.get(role):
        raise ArtifactReceiptError("artifact receipt issuer is not allowed for its role")
    try:
        verifier.verify(receipt)
    except ReceiptSignatureError as exc:
        raise ArtifactReceiptError("artifact receipt signature is invalid") from exc
    command = operation.command_json
    if (
        not isinstance(command, Mapping)
        or canonical_digest(command) != operation.command_digest
    ):
        raise ArtifactReceiptError("durable artifact command digest mismatch")
    if (
        expected_action is None
        or receipt.action != expected_action
        or receipt.command_id != command.get("command_id")
        or receipt.tenant_id != command.get("tenant_id")
        or receipt.run_id != command.get("run_id")
        or receipt.session_id != command.get("session_id")
        or receipt.artifact_id != operation.artifact_id
        or receipt.artifact_id != command.get("artifact_id")
        or receipt.producer_action_id != command.get("producer_action_id")
        or receipt.epoch != operation.epoch
        or receipt.epoch != command.get("epoch")
        or receipt.request_digest != operation.command_digest
    ):
        raise ArtifactReceiptError("artifact receipt binding mismatch")
    return receipt


def _stable_id(kind: str, *parts: object) -> str:
    material = ":".join(str(part) for part in parts)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"simverse:{kind}:{material}"))


def _wire(model_type, response: httpx.Response):
    try:
        return model_type.model_validate_json(response.content)
    except Exception as exc:  # noqa: BLE001 - all malformed service data is untrusted
        raise ArtifactReceiptError("artifact service returned an invalid wire object") from exc


def _wire_dict(model_type, value: Mapping[str, Any]):
    try:
        return model_type.model_validate_json(
            json.dumps(dict(value), separators=(",", ":"), ensure_ascii=False)
        )
    except Exception as exc:  # noqa: BLE001
        raise ArtifactReceiptError("artifact receipt has an invalid wire shape") from exc


def _operation_command(model_type, operation: LabArtifactOperation):
    command = _wire_dict(model_type, operation.command_json)
    if canonical_digest(command) != operation.command_digest:
        raise ArtifactReceiptError("durable artifact command digest mismatch")
    return command


def _stored_receipt(model_type, operation: LabArtifactOperation):
    if operation.receipt_json is None or not operation.receipt_digest:
        raise ArtifactReceiptError("durable artifact receipt is incomplete")
    receipt = _wire_dict(model_type, operation.receipt_json)
    if canonical_digest(receipt) != operation.receipt_digest:
        raise ArtifactReceiptError("durable artifact receipt digest mismatch")
    return receipt


def _issuer(
    *, issuer: str, audience: str, kid: str, key: str, ttl_seconds: int
) -> ServiceTokenIssuer:
    try:
        return ServiceTokenIssuer(JwtIssuerConfig(
            issuer=issuer,
            audience=audience,
            current_kid=kid,
            current_key=key,
            ttl_seconds=min(ttl_seconds, 900),
        ))
    except ValueError as exc:
        raise ArtifactPipelineConfigurationError(str(exc)) from exc


@dataclass(frozen=True)
class ArtifactPipelineEndpoints:
    ingest: str
    scanner: str
    cleanup: str

    def __post_init__(self) -> None:
        for name, endpoint in (
            ("ingest", self.ingest),
            ("scanner", self.scanner),
            ("cleanup", self.cleanup),
        ):
            parsed = httpx.URL(endpoint)
            if parsed.scheme not in {"http", "https"} or not parsed.host:
                raise ArtifactPipelineConfigurationError(
                    f"artifact {name} endpoint must be absolute HTTP(S)"
                )


class ArtifactPipelineClient:
    def __init__(
        self,
        *,
        endpoints: ArtifactPipelineEndpoints,
        ingest_issuer: ServiceTokenIssuer,
        scanner_issuer: ServiceTokenIssuer,
        cleanup_issuer: ServiceTokenIssuer,
        receipt_verifier: ReceiptVerifier,
        expected_receipt_issuers: Mapping[str, str],
        timeout_seconds: float,
        allowed_mime_types: frozenset[str],
        scan_policy_version: str,
        scan_deadline_seconds: int,
        upload_lease_seconds: int,
        upload_max_attempts: int,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if timeout_seconds <= 0 or scan_deadline_seconds <= 0:
            raise ArtifactPipelineConfigurationError(
                "artifact service timeouts must be positive"
            )
        if not 1 <= upload_lease_seconds <= 900:
            raise ArtifactPipelineConfigurationError(
                "artifact upload lease TTL must be between 1 and 900 seconds"
            )
        if upload_max_attempts <= 0:
            raise ArtifactPipelineConfigurationError(
                "artifact upload attempt limit must be positive"
            )
        self.endpoints = endpoints
        self.ingest_issuer = ingest_issuer
        self.scanner_issuer = scanner_issuer
        self.cleanup_issuer = cleanup_issuer
        self.receipt_verifier = receipt_verifier
        self.expected_receipt_issuers = dict(expected_receipt_issuers)
        self.allowed_mime_types = allowed_mime_types
        self.scan_policy_version = scan_policy_version
        self.scan_deadline_seconds = scan_deadline_seconds
        self.upload_lease_seconds = upload_lease_seconds
        self.upload_max_attempts = upload_max_attempts
        self._owns_client = http_client is None
        self.http = http_client or httpx.AsyncClient(timeout=timeout_seconds)

    @classmethod
    def from_settings(cls) -> "ArtifactPipelineClient":
        if not settings.lab_artifact_pipeline_enabled:
            raise ArtifactPipelineConfigurationError(
                "production artifact pipeline is disabled"
            )
        if (
            settings.lab_artifact_pending_ttl_hours <= 0
            or settings.lab_artifact_quarantine_ttl_days <= 0
            or settings.lab_artifact_retention_days <= 0
        ):
            raise ArtifactPipelineConfigurationError(
                "artifact lifecycle TTLs must be positive"
            )
        receipt_verifier, expected_receipt_issuers, receipt_keys = (
            _load_receipt_trust()
        )
        auth_planes = (
            (
                "ingest",
                settings.lab_artifact_ingest_auth_current_kid,
                settings.lab_artifact_ingest_auth_current_key,
                settings.lab_artifact_ingest_auth_next_kid,
                settings.lab_artifact_ingest_auth_next_key,
            ),
            (
                "scanner",
                settings.lab_artifact_scanner_auth_current_kid,
                settings.lab_artifact_scanner_auth_current_key,
                settings.lab_artifact_scanner_auth_next_kid,
                settings.lab_artifact_scanner_auth_next_key,
            ),
            (
                "cleanup",
                settings.lab_artifact_cleanup_auth_current_kid,
                settings.lab_artifact_cleanup_auth_current_key,
                settings.lab_artifact_cleanup_auth_next_kid,
                settings.lab_artifact_cleanup_auth_next_key,
            ),
        )
        expected_audiences = {
            "ingest": "lab-artifact-ingest",
            "scanner": "lab-artifact-scanner",
            "cleanup": "lab-artifact-cleanup",
        }
        configured_audiences = {
            "ingest": settings.lab_artifact_ingest_auth_audience,
            "scanner": settings.lab_artifact_scanner_auth_audience,
            "cleanup": settings.lab_artifact_cleanup_auth_audience,
        }
        if configured_audiences != expected_audiences:
            raise ArtifactPipelineConfigurationError(
                "artifact service JWT audiences must match their dedicated roles"
            )
        auth_keys: list[str] = []
        for name, current_kid, current_key, next_kid, next_key in auth_planes:
            if any(
                not value
                for value in (current_kid, current_key, next_kid, next_key)
            ):
                raise ArtifactPipelineConfigurationError(
                    f"artifact {name} auth keyring is incomplete"
                )
            if current_kid == next_kid or current_key == next_key:
                raise ArtifactPipelineConfigurationError(
                    f"artifact {name} auth keys must be distinct"
                )
            if current_key in receipt_keys or next_key in receipt_keys:
                raise ArtifactPipelineConfigurationError(
                    f"artifact {name} JWT keys must not be receipt signing keys"
                )
            auth_keys.extend((current_key, next_key))
        if len(set(auth_keys)) != len(auth_keys):
            raise ArtifactPipelineConfigurationError(
                "artifact JWT keys must not be reused across service roles"
            )
        ttl = settings.lab_artifact_upload_lease_ttl_s
        return cls(
            endpoints=ArtifactPipelineEndpoints(
                ingest=settings.lab_artifact_ingest_base_url.rstrip("/"),
                scanner=settings.lab_artifact_scanner_base_url.rstrip("/"),
                cleanup=settings.lab_artifact_cleanup_base_url.rstrip("/"),
            ),
            ingest_issuer=_issuer(
                issuer=settings.lab_artifact_ingest_auth_issuer,
                audience=settings.lab_artifact_ingest_auth_audience,
                kid=settings.lab_artifact_ingest_auth_current_kid,
                key=settings.lab_artifact_ingest_auth_current_key,
                ttl_seconds=ttl,
            ),
            scanner_issuer=_issuer(
                issuer=settings.lab_artifact_scanner_auth_issuer,
                audience=settings.lab_artifact_scanner_auth_audience,
                kid=settings.lab_artifact_scanner_auth_current_kid,
                key=settings.lab_artifact_scanner_auth_current_key,
                ttl_seconds=ttl,
            ),
            cleanup_issuer=_issuer(
                issuer=settings.lab_artifact_cleanup_auth_issuer,
                audience=settings.lab_artifact_cleanup_auth_audience,
                kid=settings.lab_artifact_cleanup_auth_current_kid,
                key=settings.lab_artifact_cleanup_auth_current_key,
                ttl_seconds=ttl,
            ),
            receipt_verifier=receipt_verifier,
            expected_receipt_issuers=expected_receipt_issuers,
            timeout_seconds=settings.lab_artifact_service_timeout_s,
            allowed_mime_types=frozenset(settings.lab_artifact_allowed_mime_types),
            scan_policy_version=settings.lab_artifact_scan_policy_version,
            scan_deadline_seconds=settings.lab_artifact_scan_deadline_s,
            upload_lease_seconds=ttl,
            upload_max_attempts=settings.lab_artifact_upload_max_attempts,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self.http.aclose()

    async def ready(self) -> bool:
        for endpoint in (
            self.endpoints.ingest,
            self.endpoints.scanner,
            self.endpoints.cleanup,
        ):
            try:
                response = await self.http.get(f"{endpoint}/readyz")
                payload = response.json()
            except (httpx.HTTPError, ValueError):
                return False
            if (
                response.status_code != 200
                or not isinstance(payload, dict)
                or payload.get("ready") is not True
            ):
                return False
        return True

    @staticmethod
    def _binding(
        artifact: LabArtifact, *, operation_id: str
    ) -> RequestBinding:
        if (
            not artifact.tenant_id
            or not artifact.provider_session_id
            or not artifact.provider_artifact_id
        ):
            raise ArtifactPipelineError("artifact producer binding is incomplete")
        return RequestBinding(
            tenant_id=artifact.tenant_id,
            run_id=artifact.run_id,
            session_id=artifact.provider_session_id,
            artifact_id=artifact.id,
            producer_action_id=artifact.producer_action_id,
            epoch=artifact.producer_epoch,
            operation_id=operation_id,
        )

    def _verify_receipt(self, receipt, *, operation: LabArtifactOperation):
        return _verify_receipt_binding(
            receipt,
            operation=operation,
            verifier=self.receipt_verifier,
            expected_issuers=self.expected_receipt_issuers,
        )

    async def create_upload_lease(
        self, db, *, artifact: LabArtifact, max_bytes: int
    ) -> tuple[ArtifactUploadLease, LabArtifactOperation]:
        now = datetime.now(UTC)
        latest = await db.scalar(
            select(LabArtifactOperation)
            .where(
                LabArtifactOperation.artifact_id == artifact.id,
                LabArtifactOperation.operation_type == "upload",
            )
            .order_by(LabArtifactOperation.created_at.desc())
            .limit(1)
        )
        operation = None
        if latest is not None:
            previous_command = _operation_command(UploadLeaseCommand, latest)
            if previous_command.upload_id != latest.operation_id:
                raise ArtifactReceiptError(
                    "durable upload operation ID does not match its command"
                )
            if latest.state == "succeeded":
                raise ArtifactPipelineError(
                    "artifact upload already has a successful operation"
                )
            if latest.state == "quarantined":
                raise ArtifactPipelineError("artifact upload is quarantined")
            if latest.state == "processing" and latest.receipt_json is not None:
                receipt = self._verify_receipt(
                    _stored_receipt(UploadLeaseReceipt, latest), operation=latest
                )
                return self._runtime_upload_lease(
                    command=previous_command, receipt=receipt
                ), latest
            if latest.state == "pending" and previous_command.expires_at > now:
                operation = latest
            else:
                latest.state = "failed"
                latest.error_code = (
                    latest.error_code or "upload_lease_expired"
                )

        attempt = 1 if latest is None else max(1, latest.attempt) + 1
        if operation is None:
            if attempt > self.upload_max_attempts:
                artifact.scan_status = "failed"
                artifact.verification_status = "rejected"
                artifact.scan_error_code = "upload_retry_limit_exhausted"
                await db.commit()
                raise ArtifactPipelineError("artifact upload retry limit exhausted")
            upload_id = _stable_id(
                "artifact-upload", artifact.id, artifact.producer_epoch, attempt
            )
            expires_at = datetime.now(UTC) + timedelta(
                seconds=self.upload_lease_seconds
            )
            command = UploadLeaseCommand(
                command_id=_stable_id("artifact-upload-command", upload_id),
                tenant_id=artifact.tenant_id,
                run_id=artifact.run_id,
                session_id=artifact.provider_session_id,
                artifact_id=artifact.id,
                producer_action_id=artifact.producer_action_id,
                epoch=artifact.producer_epoch,
                upload_id=upload_id,
                content_type=(
                    artifact.declared_content_type
                    or artifact.content_type
                    or "application/octet-stream"
                ),
                max_bytes=max_bytes,
                expected_sha256=artifact.expected_sha256,
                declared_byte_size=artifact.declared_byte_size,
                expires_at=expires_at,
            )
            command_json = command.model_dump(mode="json")
            operation = LabArtifactOperation(
                operation_id=upload_id,
                artifact_id=artifact.id,
                operation_type="upload",
                state="pending",
                epoch=artifact.producer_epoch,
                command_digest=canonical_digest(command),
                command_json=command_json,
                service_endpoint=self.endpoints.ingest,
                job_id=upload_id,
                attempt=attempt,
            )
            db.add(operation)
            await db.commit()
            await db.refresh(operation)
        command = _operation_command(UploadLeaseCommand, operation)
        upload_id = command.upload_id
        if upload_id != operation.operation_id:
            raise ArtifactReceiptError(
                "durable upload operation ID does not match its command"
            )
        binding = RequestBinding.from_command(command, operation_id=upload_id)
        token = self.ingest_issuer.issue(
            action="artifact.lease.create", binding=binding
        )
        try:
            response = await self.http.post(
                f"{self.endpoints.ingest}/v1/upload-leases",
                json=command.model_dump(mode="json"),
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ArtifactPipelineError("artifact ingest lease request failed") from exc
        receipt = self._verify_receipt(
            _wire(UploadLeaseReceipt, response), operation=operation
        )
        if receipt.upload_id != upload_id:
            raise ArtifactReceiptError("artifact lease upload ID mismatch")
        operation.state = "processing"
        operation.receipt_json = receipt.model_dump(mode="json")
        operation.receipt_digest = canonical_digest(receipt)
        await db.commit()
        return self._runtime_upload_lease(command=command, receipt=receipt), operation

    def _runtime_upload_lease(
        self,
        *,
        command: UploadLeaseCommand,
        receipt: UploadLeaseReceipt,
    ) -> ArtifactUploadLease:
        if receipt.upload_id != command.upload_id:
            raise ArtifactReceiptError("artifact lease upload ID mismatch")
        return ArtifactUploadLease(
            upload_id=command.upload_id,
            artifact_id=command.artifact_id,
            tenant_id=command.tenant_id,
            run_id=command.run_id,
            session_id=command.session_id,
            producer_action_id=command.producer_action_id,
            epoch=command.epoch,
            upload_url=(
                f"{self.endpoints.ingest}/v1/uploads/{command.upload_id}"
            ),
            bearer_token=receipt.upload_token,
            max_bytes=receipt.max_bytes,
            content_type=command.content_type,
            expected_sha256=command.expected_sha256,
            expires_at=receipt.expires_at,
        )

    async def query_upload_receipt(
        self, *, operation: LabArtifactOperation
    ) -> UploadReceipt:
        if operation.operation_type != "upload":
            raise ArtifactPipelineError("operation is not an upload")
        command = _operation_command(UploadLeaseCommand, operation)
        if command.upload_id != operation.operation_id:
            raise ArtifactReceiptError(
                "durable upload operation ID does not match its command"
            )
        binding = RequestBinding.from_command(
            command, operation_id=command.upload_id
        )
        token = self.ingest_issuer.issue(
            action="artifact.upload.read", binding=binding
        )
        try:
            response = await self.http.get(
                f"{self.endpoints.ingest}/v1/uploads/{command.upload_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise ArtifactPipelineError(
                "artifact upload receipt request failed"
            ) from exc
        if response.status_code == 404:
            raise ArtifactOperationNotFound("artifact upload is unknown to Ingest")
        if response.status_code == 409:
            raise ArtifactOperationPending("artifact upload is not terminal")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ArtifactPipelineError(
                "artifact upload receipt request failed"
            ) from exc
        receipt = self._verify_receipt(
            _wire(UploadReceipt, response), operation=operation
        )
        if receipt.upload_id != command.upload_id:
            raise ArtifactReceiptError("artifact upload receipt ID mismatch")
        return receipt

    async def fail_upload_attempt(
        self,
        db,
        *,
        operation: LabArtifactOperation,
        error_code: str,
    ) -> None:
        if operation.operation_type != "upload" or operation.state == "succeeded":
            raise ArtifactPipelineError("upload operation cannot be failed")
        operation.state = "failed"
        operation.error_code = error_code[:100]
        operation.next_retry_at = None
        await db.commit()

    async def apply_upload_receipt(
        self,
        db,
        *,
        receipt_value: Mapping[str, Any],
        commit: bool = True,
    ) -> LabArtifact:
        receipt = _wire_dict(UploadReceipt, receipt_value)
        operation = await db.scalar(
            select(LabArtifactOperation)
            .where(LabArtifactOperation.operation_id == receipt.upload_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if operation is None or operation.operation_type != "upload":
            raise ArtifactReceiptError("upload receipt has no durable operation")
        self._verify_receipt(receipt, operation=operation)
        incoming_digest = canonical_digest(receipt)
        if operation.state in {"succeeded", "failed", "quarantined"}:
            if operation.receipt_digest != incoming_digest:
                raise ArtifactReceiptError(
                    "terminal upload operation was rebound to another receipt"
                )
            artifact = await self._lock_artifact(db, operation.artifact_id)
            if operation.state == "succeeded":
                return artifact
            raise ArtifactPipelineError(
                operation.error_code or "upload_terminal_failure"
            )
        artifact = await self._lock_artifact(db, operation.artifact_id)
        operation.receipt_json = receipt.model_dump(mode="json")
        operation.receipt_digest = incoming_digest
        if receipt.status != "completed" or receipt.quarantine_ref is None:
            operation.state = "failed"
            operation.error_code = receipt.error_code or "upload_failed"
            if receipt.quarantine_ref is not None:
                ref = receipt.quarantine_ref
                artifact.sha256 = ref.sha256
                artifact.byte_size = ref.byte_size
                artifact.content_type = ref.content_type
                artifact.storage_backend = ref.backend
                artifact.storage_status = "quarantined"
                artifact.quarantine_bucket = ref.bucket
                artifact.quarantine_key = ref.key
                artifact.quarantine_version_id = ref.version_id
                artifact.quarantine_etag = ref.etag
                artifact.upload_receipt_digest = operation.receipt_digest
                artifact.expires_at = datetime.now(UTC) + timedelta(
                    days=settings.lab_artifact_quarantine_ttl_days
                )
            artifact.scan_status = "failed"
            artifact.verification_status = "rejected"
            artifact.scan_error_code = operation.error_code
            if commit:
                await db.commit()
            raise ArtifactPipelineError(operation.error_code)
        ref = receipt.quarantine_ref
        artifact.sha256 = ref.sha256
        artifact.byte_size = ref.byte_size
        artifact.content_type = ref.content_type
        artifact.storage_backend = ref.backend
        artifact.storage_status = "quarantined"
        artifact.quarantine_bucket = ref.bucket
        artifact.quarantine_key = ref.key
        artifact.quarantine_version_id = ref.version_id
        artifact.quarantine_etag = ref.etag
        artifact.upload_receipt_digest = operation.receipt_digest
        artifact.expires_at = datetime.now(UTC) + timedelta(
            days=settings.lab_artifact_quarantine_ttl_days
        )
        artifact.uri = None
        artifact.text_md = None
        if ref.content_type not in self.allowed_mime_types:
            operation.state = "quarantined"
            operation.error_code = "content_type_not_allowed"
            artifact.scan_status = "flagged"
            artifact.verification_status = "rejected"
            if commit:
                await db.commit()
            raise ArtifactPipelineError("content_type_not_allowed")
        artifact.scan_status = "pending"
        artifact.verification_status = "unverified"
        operation.state = "succeeded"
        operation.error_code = None
        if commit:
            await db.commit()
            await db.refresh(artifact)
        return artifact

    @staticmethod
    def _quarantine_ref(artifact: LabArtifact) -> ObjectRef:
        fields = (
            artifact.storage_backend,
            artifact.quarantine_bucket,
            artifact.quarantine_key,
            artifact.quarantine_version_id,
            artifact.quarantine_etag,
            artifact.sha256,
            artifact.content_type,
        )
        if any(not field for field in fields):
            raise ArtifactPipelineError("artifact quarantine locator is incomplete")
        return ObjectRef(
            backend=artifact.storage_backend,
            zone="quarantine",
            bucket=artifact.quarantine_bucket,
            key=artifact.quarantine_key,
            version_id=artifact.quarantine_version_id,
            etag=artifact.quarantine_etag,
            byte_size=artifact.byte_size,
            sha256=artifact.sha256,
            content_type=artifact.content_type,
        )

    @staticmethod
    def _released_ref(artifact: LabArtifact) -> ObjectRef:
        fields = (
            artifact.storage_backend,
            artifact.released_bucket,
            artifact.released_key,
            artifact.released_version_id,
            artifact.released_etag,
            artifact.sha256,
            artifact.content_type,
        )
        if any(not field for field in fields):
            raise ArtifactPipelineError("artifact released locator is incomplete")
        return ObjectRef(
            backend=artifact.storage_backend,
            zone="released",
            bucket=artifact.released_bucket,
            key=artifact.released_key,
            version_id=artifact.released_version_id,
            etag=artifact.released_etag,
            byte_size=artifact.byte_size,
            sha256=artifact.sha256,
            content_type=artifact.content_type,
        )

    @staticmethod
    async def _lock_artifact(db, artifact_id: str) -> LabArtifact:
        artifact = await db.scalar(
            select(LabArtifact)
            .where(LabArtifact.id == artifact_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if artifact is None:
            raise ArtifactPipelineError("artifact no longer exists")
        return artifact

    @staticmethod
    def _record_scan_attempt(
        *,
        artifact: LabArtifact,
        operation: LabArtifactOperation,
        command: ScanCommand,
    ) -> None:
        if artifact.scan_job_id != command.scan_job_id:
            artifact.scan_job_id = command.scan_job_id
            artifact.scan_policy_version = command.policy_version
            artifact.scan_attempts += 1
        operation.attempt = max(operation.attempt, 1)

    async def submit_scan(self, db, *, artifact: LabArtifact) -> ScanReceipt:
        artifact = await self._lock_artifact(db, artifact.id)
        if artifact.storage_status != "quarantined":
            raise ArtifactPipelineError(
                "artifact must remain quarantined while a scan is submitted"
            )
        scan_job_id = _stable_id(
            "artifact-scan",
            artifact.id,
            artifact.sha256,
            artifact.producer_epoch,
            artifact.scan_attempts + 1,
        )
        operation = await db.scalar(
            select(LabArtifactOperation).where(
                LabArtifactOperation.operation_id == scan_job_id
            )
        )
        if operation is None:
            command = ScanCommand(
                command_id=_stable_id("artifact-scan-command", scan_job_id),
                tenant_id=artifact.tenant_id,
                run_id=artifact.run_id,
                session_id=artifact.provider_session_id,
                artifact_id=artifact.id,
                producer_action_id=artifact.producer_action_id,
                epoch=artifact.producer_epoch,
                scan_job_id=scan_job_id,
                quarantine_ref=self._quarantine_ref(artifact),
                sha256=artifact.sha256,
                byte_size=artifact.byte_size,
                content_type=artifact.content_type,
                policy_version=self.scan_policy_version,
                deadline_at=datetime.now(UTC) + timedelta(
                    seconds=self.scan_deadline_seconds
                ),
            )
            operation = LabArtifactOperation(
                operation_id=scan_job_id,
                artifact_id=artifact.id,
                operation_type="scan",
                state="pending",
                epoch=artifact.producer_epoch,
                command_digest=canonical_digest(command),
                command_json=command.model_dump(mode="json"),
                service_endpoint=self.endpoints.scanner,
                job_id=scan_job_id,
            )
            db.add(operation)
            await db.commit()
            await db.refresh(operation)
        command = _operation_command(ScanCommand, operation)
        if (
            command.scan_job_id != operation.operation_id
            or operation.artifact_id != artifact.id
        ):
            raise ArtifactReceiptError(
                "durable scan operation does not match its command"
            )
        binding = RequestBinding.from_command(command, operation_id=scan_job_id)
        token = self.scanner_issuer.issue(
            action="artifact.scan.submit", binding=binding
        )
        try:
            response = await self.http.post(
                f"{self.endpoints.scanner}/v1/scans",
                json=command.model_dump(mode="json"),
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ArtifactPipelineError("artifact scan submission failed") from exc
        receipt = self._verify_receipt(_wire(ScanReceipt, response), operation=operation)
        operation = await db.scalar(
            select(LabArtifactOperation)
            .where(LabArtifactOperation.id == operation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if operation is None:
            raise ArtifactPipelineError("scan operation no longer exists")
        artifact = await self._lock_artifact(db, artifact.id)
        if artifact.storage_status != "quarantined":
            raise ArtifactPipelineError(
                "artifact left quarantine while scan was in flight"
            )
        self._record_scan_attempt(
            artifact=artifact, operation=operation, command=command
        )
        self._apply_scan_receipt(
            artifact=artifact,
            operation=operation,
            command=command,
            receipt=receipt,
        )
        await db.commit()
        return receipt

    def _apply_scan_receipt(
        self,
        *,
        artifact: LabArtifact,
        operation: LabArtifactOperation,
        command: ScanCommand,
        receipt: ScanReceipt,
    ) -> None:
        quarantine = self._quarantine_ref(artifact)
        if (
            receipt.scan_job_id != command.scan_job_id
            or receipt.policy_version != command.policy_version
            or receipt.quarantine_ref != command.quarantine_ref
            or command.quarantine_ref != quarantine
        ):
            raise ArtifactReceiptError("scan receipt exact-object binding mismatch")

        operation.receipt_json = receipt.model_dump(mode="json")
        operation.receipt_digest = canonical_digest(receipt)
        operation.next_retry_at = None
        if receipt.status in {"pending", "running"}:
            operation.state = "processing"
            artifact.scan_status = "scanning"
            return
        if receipt.status == "clean" and receipt.released_ref is not None:
            released = receipt.released_ref
            if (
                released.backend != artifact.storage_backend
                or released.sha256 != artifact.sha256
                or released.byte_size != artifact.byte_size
                or released.content_type != artifact.content_type
            ):
                raise ArtifactReceiptError("scan promotion changed artifact bytes")
            artifact.released_bucket = released.bucket
            artifact.released_key = released.key
            artifact.released_version_id = released.version_id
            artifact.released_etag = released.etag
            artifact.storage_status = "released"
            artifact.scan_status = "clean"
            artifact.verification_status = "verified"
            artifact.scan_engine_version = receipt.scan_engine_version
            artifact.scanned_at = receipt.completed_at
            artifact.released_at = receipt.completed_at
            artifact.expires_at = datetime.now(UTC) + timedelta(
                days=settings.lab_artifact_retention_days
            )
            artifact.scan_receipt_digest = canonical_digest(receipt)
            artifact.scan_error_code = None
            operation.state = "succeeded"
            operation.error_code = None
            return
        if receipt.status == "flagged":
            artifact.scan_status = "flagged"
            artifact.verification_status = "rejected"
            artifact.scan_engine_version = receipt.scan_engine_version
            artifact.scanned_at = receipt.completed_at
            artifact.scan_receipt_digest = canonical_digest(receipt)
            artifact.scan_error_code = receipt.error_code
            artifact.expires_at = datetime.now(UTC) + timedelta(
                days=settings.lab_artifact_quarantine_ttl_days
            )
            operation.state = "quarantined"
            operation.error_code = receipt.error_code
            return

        artifact.scan_status = "failed"
        artifact.verification_status = "unverified"
        artifact.scan_engine_version = receipt.scan_engine_version
        artifact.scanned_at = receipt.completed_at
        artifact.scan_receipt_digest = canonical_digest(receipt)
        artifact.scan_error_code = receipt.error_code or "scan_failed"
        artifact.expires_at = datetime.now(UTC) + timedelta(
            days=settings.lab_artifact_quarantine_ttl_days
        )
        operation.state = "failed"
        operation.error_code = artifact.scan_error_code
        if artifact.scan_attempts < settings.lab_artifact_scan_max_attempts:
            operation.next_retry_at = datetime.now(UTC) + timedelta(
                seconds=min(300, 2 ** min(artifact.scan_attempts, 8))
            )

    async def poll_scan(
        self, db, *, operation: LabArtifactOperation
    ) -> ScanReceipt:
        command = _operation_command(ScanCommand, operation)
        if command.scan_job_id != operation.operation_id:
            raise ArtifactReceiptError(
                "durable scan operation ID does not match its command"
            )
        binding = RequestBinding.from_command(
            command, operation_id=command.scan_job_id
        )
        token = self.scanner_issuer.issue(
            action="artifact.scan.read", binding=binding
        )
        try:
            response = await self.http.get(
                f"{self.endpoints.scanner}/v1/scans/{command.scan_job_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise ArtifactPipelineError("artifact scan status request failed") from exc
        if response.status_code == 404:
            raise ArtifactOperationNotFound("artifact scan job was not found")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ArtifactPipelineError("artifact scan status request failed") from exc
        receipt = self._verify_receipt(_wire(ScanReceipt, response), operation=operation)
        operation = await db.scalar(
            select(LabArtifactOperation)
            .where(LabArtifactOperation.id == operation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if operation is None:
            raise ArtifactPipelineError("scan operation no longer exists")
        artifact = await self._lock_artifact(db, operation.artifact_id)
        if artifact.storage_status != "quarantined":
            raise ArtifactPipelineError(
                "artifact left quarantine while scan was in flight"
            )
        self._record_scan_attempt(
            artifact=artifact, operation=operation, command=command
        )
        self._apply_scan_receipt(
            artifact=artifact,
            operation=operation,
            command=command,
            receipt=receipt,
        )
        await db.commit()
        return receipt

    def _delete_targets(self, artifact: LabArtifact) -> list[DeleteTarget]:
        targets: list[DeleteTarget] = []
        if artifact.quarantine_version_id:
            targets.append(DeleteTarget(object_ref=self._quarantine_ref(artifact)))
        if artifact.released_version_id:
            targets.append(DeleteTarget(object_ref=self._released_ref(artifact)))
        if not targets:
            raise ArtifactPipelineError("artifact has no exact object version to delete")
        return targets

    @staticmethod
    async def _has_active_hold(db, artifact: LabArtifact) -> bool:
        from app.models.lab_task import LabTask
        from app.models.world_change_proposal import WorldChangeProposal

        active = await db.scalar(
            select(LabArtifactHold.id)
            .where(
                LabArtifactHold.artifact_id == artifact.id,
                LabArtifactHold.released_at.is_(None),
            )
            .limit(1)
        )
        if active is not None or bool(artifact.retention_hold):
            return True
        completed_task = await db.scalar(
            select(LabTask.id)
            .where(
                LabTask.id == artifact.task_id,
                LabTask.status == "completed",
            )
            .limit(1)
        )
        if completed_task is not None:
            return True
        proposal = await db.scalar(
            select(WorldChangeProposal.id)
            .where(
                WorldChangeProposal.origin == "lab_run",
                WorldChangeProposal.origin_ref == artifact.run_id,
            )
            .limit(1)
        )
        return proposal is not None

    async def _stage_delete(
        self,
        db,
        *,
        artifact: LabArtifact,
        quarantine_residue_only: bool,
    ) -> LabArtifactOperation:
        artifact = await self._lock_artifact(db, artifact.id)
        if artifact.storage_status == "deleted":
            raise ArtifactPipelineError("artifact is already deleted")
        active_delete = await db.scalar(
            select(LabArtifactOperation)
            .where(
                LabArtifactOperation.artifact_id == artifact.id,
                LabArtifactOperation.operation_type == "delete",
                LabArtifactOperation.state.in_(("pending", "processing")),
            )
            .order_by(LabArtifactOperation.created_at)
            .limit(1)
        )
        if active_delete is not None:
            active_command = _operation_command(DeleteCommand, active_delete)
            expected_purpose = (
                "quarantine_residue"
                if quarantine_residue_only
                else "retention_expiry"
            )
            if active_command.purpose == expected_purpose:
                return active_delete
            raise ArtifactPipelineError(
                "artifact cleanup is fenced by another delete purpose"
            )
        active_scan = await db.scalar(
            select(LabArtifactOperation.id)
            .where(
                LabArtifactOperation.artifact_id == artifact.id,
                LabArtifactOperation.operation_type == "scan",
                LabArtifactOperation.state.in_(("pending", "processing")),
            )
            .limit(1)
        )
        if active_scan is not None:
            raise ArtifactPipelineError(
                "artifact cleanup is fenced by an active scan"
            )
        if await self._has_active_hold(db, artifact):
            raise ArtifactPipelineError(
                "artifact cleanup is fenced by an active retention hold"
            )
        if quarantine_residue_only:
            if (
                artifact.storage_status != "released"
                or not artifact.released_version_id
                or not artifact.quarantine_version_id
            ):
                raise ArtifactPipelineError(
                    "artifact has no releasable quarantine promotion residue"
                )
            targets = [
                DeleteTarget(object_ref=self._quarantine_ref(artifact))
            ]
            purpose = "quarantine_residue"
        else:
            targets = self._delete_targets(artifact)
            purpose = "retention_expiry"
        prior_deletes = (
            await db.execute(
                select(LabArtifactOperation)
                .where(
                    LabArtifactOperation.artifact_id == artifact.id,
                    LabArtifactOperation.operation_type == "delete",
                )
                .order_by(LabArtifactOperation.created_at.desc())
            )
        ).scalars().all()
        purpose_attempts = [
            prior.attempt
            for prior in prior_deletes
            if _operation_command(DeleteCommand, prior).purpose == purpose
        ]
        attempt = max(purpose_attempts, default=0)
        if attempt >= settings.lab_artifact_cleanup_max_attempts:
            raise ArtifactPipelineError("artifact cleanup retry limit exhausted")
        delete_id = _stable_id(
            "artifact-delete", artifact.id, purpose, attempt + 1
        )
        existing = await db.scalar(
            select(LabArtifactOperation).where(
                LabArtifactOperation.operation_id == delete_id
            )
        )
        if existing is not None:
            return existing
        command = DeleteCommand(
            command_id=_stable_id("artifact-delete-command", delete_id),
            tenant_id=artifact.tenant_id,
            run_id=artifact.run_id,
            session_id=artifact.provider_session_id,
            artifact_id=artifact.id,
            producer_action_id=artifact.producer_action_id,
            epoch=artifact.producer_epoch,
            delete_operation_id=delete_id,
            purpose=purpose,
            targets=targets,
            deadline_at=datetime.now(UTC) + timedelta(
                seconds=self.scan_deadline_seconds
            ),
        )
        operation = LabArtifactOperation(
            operation_id=delete_id,
            artifact_id=artifact.id,
            operation_type="delete",
            state="pending",
            epoch=artifact.producer_epoch,
            command_digest=canonical_digest(command),
            command_json=command.model_dump(mode="json"),
            service_endpoint=self.endpoints.cleanup,
            job_id=delete_id,
            attempt=attempt + 1,
        )
        db.add(operation)
        db.add(OutboxEvent(
            event_id=delete_id,
            tenant_id=artifact.tenant_id or "system",
            run_id=artifact.run_id,
            topic="artifact.cleanup.requested",
            payload_json={
                "operation_id": delete_id,
                "artifact_id": artifact.id,
                "purpose": purpose,
            },
        ))
        if not quarantine_residue_only:
            artifact.storage_status = "delete_pending"
        await db.commit()
        await db.refresh(operation)
        return operation

    async def stage_delete(self, db, *, artifact: LabArtifact) -> LabArtifactOperation:
        return await self._stage_delete(
            db, artifact=artifact, quarantine_residue_only=False
        )

    async def stage_quarantine_cleanup(
        self, db, *, artifact: LabArtifact
    ) -> LabArtifactOperation:
        return await self._stage_delete(
            db, artifact=artifact, quarantine_residue_only=True
        )

    async def submit_delete(
        self, db, *, operation: LabArtifactOperation
    ) -> DeleteReceipt:
        command = _operation_command(DeleteCommand, operation)
        if command.delete_operation_id != operation.operation_id:
            raise ArtifactReceiptError(
                "durable delete operation ID does not match its command"
            )
        operation = await db.scalar(
            select(LabArtifactOperation)
            .where(LabArtifactOperation.id == operation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if operation is None or operation.state != "pending":
            raise ArtifactPipelineError("delete operation is not pending")
        artifact = await self._lock_artifact(db, operation.artifact_id)
        target_zones = {target.object_ref.zone for target in command.targets}
        quarantine_residue_only = command.purpose == "quarantine_residue"
        if quarantine_residue_only:
            if target_zones != {"quarantine"}:
                operation.state = "quarantined"
                operation.error_code = "delete_purpose_target_mismatch"
                operation.next_retry_at = None
                await db.commit()
                raise ArtifactPipelineError(
                    "quarantine residue command contains a non-quarantine target"
                )
            if artifact.storage_status != "released":
                operation.state = "quarantined"
                operation.error_code = "artifact_not_released"
                operation.next_retry_at = None
                await db.commit()
                raise ArtifactPipelineError(
                    "quarantine residue cleanup requires a released artifact"
                )
            if await self._has_active_hold(db, artifact):
                operation.state = "quarantined"
                operation.error_code = "retention_hold_added"
                operation.next_retry_at = None
                await db.commit()
                raise ArtifactPipelineError(
                    "artifact residue deletion was cancelled by a retention hold"
                )
            current_targets = [
                DeleteTarget(object_ref=self._quarantine_ref(artifact))
            ]
        else:
            if command.purpose != "retention_expiry":
                operation.state = "quarantined"
                operation.error_code = "delete_purpose_invalid"
                operation.next_retry_at = None
                await db.commit()
                raise ArtifactPipelineError("delete command purpose is invalid")
            if artifact.storage_status != "delete_pending":
                operation.state = "quarantined"
                operation.error_code = "artifact_not_delete_pending"
                operation.next_retry_at = None
                await db.commit()
                raise ArtifactPipelineError("artifact is not delete-pending")
            if await self._has_active_hold(db, artifact):
                operation.state = "quarantined"
                operation.error_code = "retention_hold_added"
                operation.next_retry_at = None
                artifact.storage_status = (
                    "released"
                    if artifact.released_version_id
                    else "quarantined"
                )
                await db.commit()
                raise ArtifactPipelineError(
                    "artifact deletion was cancelled by a retention hold"
                )
            current_targets = self._delete_targets(artifact)
        requested = {
            canonical_digest(target.object_ref) for target in command.targets
        }
        current = {
            canonical_digest(target.object_ref) for target in current_targets
        }
        if requested != current:
            operation.state = "quarantined"
            operation.error_code = "delete_target_superseded"
            operation.next_retry_at = None
            if artifact.storage_status == "delete_pending":
                artifact.storage_status = (
                    "released"
                    if artifact.released_version_id
                    else "quarantined"
                )
            await db.commit()
            raise ArtifactPipelineError(
                "delete command no longer matches current exact locators"
            )
        binding = RequestBinding.from_command(
            command, operation_id=command.delete_operation_id
        )
        token = self.cleanup_issuer.issue(action="artifact.delete", binding=binding)
        try:
            response = await self.http.post(
                f"{self.endpoints.cleanup}/v1/deletes",
                json=command.model_dump(mode="json"),
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise ArtifactPipelineError("artifact delete request failed") from exc
        receipt = self._verify_receipt(_wire(DeleteReceipt, response), operation=operation)
        if receipt.delete_operation_id != command.delete_operation_id:
            raise ArtifactReceiptError("delete receipt operation ID mismatch")
        operation.receipt_json = receipt.model_dump(mode="json")
        operation.receipt_digest = canonical_digest(receipt)
        if receipt.status == "completed":
            expected = requested
            actual = {canonical_digest(proof.object_ref) for proof in receipt.proofs}
            if actual != expected:
                operation.state = "quarantined"
                operation.error_code = "delete_receipt_incomplete"
                operation.next_retry_at = None
                await db.commit()
                raise ArtifactReceiptError("delete receipt omitted an exact version")
            operation.state = "succeeded"
            operation.error_code = None
            operation.next_retry_at = None
            artifact.quarantine_bucket = None
            artifact.quarantine_key = None
            artifact.quarantine_version_id = None
            artifact.quarantine_etag = None
            if not quarantine_residue_only:
                artifact.storage_status = "deleted"
                artifact.deleted_at = receipt.completed_at
                artifact.released_bucket = None
                artifact.released_key = None
                artifact.released_version_id = None
                artifact.released_etag = None
                artifact.uri = None
                artifact.text_md = None
            db.add(OutboxEvent(
                event_id=receipt.receipt_id,
                tenant_id=artifact.tenant_id or "system",
                run_id=artifact.run_id,
                topic="artifact.cleanup.completed",
                payload_json={
                    "operation_id": operation.operation_id,
                    "artifact_id": artifact.id,
                    "receipt_digest": operation.receipt_digest,
                    "purpose": (
                        "quarantine_residue"
                        if quarantine_residue_only
                        else "retention_expiry"
                    ),
                },
            ))
        else:
            operation.state = "failed"
            operation.error_code = receipt.error_code or "delete_failed"
            operation.next_retry_at = (
                datetime.now(UTC)
                + timedelta(seconds=min(300, 2 ** min(operation.attempt, 8)))
                if operation.attempt < settings.lab_artifact_cleanup_max_attempts
                else None
            )
        await db.commit()
        return receipt

    async def reconcile_once(self, session_factory, *, limit: int = 50) -> dict[str, int]:
        stats = {
            "upload_reconciled": 0,
            "upload_pending": 0,
            "scan_submitted": 0,
            "scan_polled": 0,
            "delete_submitted": 0,
            "delete_retried": 0,
            "residue_staged": 0,
            "terminal_failures": 0,
            "errors": 0,
        }
        async with session_factory() as db:
            uploads = (
                await db.execute(
                    select(LabArtifactOperation)
                    .where(
                        LabArtifactOperation.operation_type == "upload",
                        LabArtifactOperation.state.in_(("pending", "processing")),
                        (
                            LabArtifactOperation.next_retry_at.is_(None)
                            | (
                                LabArtifactOperation.next_retry_at
                                <= datetime.now(UTC)
                            )
                        ),
                    )
                    .order_by(LabArtifactOperation.created_at)
                    .limit(limit)
                )
            ).scalars().all()
            for operation in uploads:
                operation_row_id = operation.id
                try:
                    command = _operation_command(UploadLeaseCommand, operation)
                    receipt = await self.query_upload_receipt(operation=operation)
                    try:
                        await self.apply_upload_receipt(
                            db, receipt_value=receipt.model_dump(mode="json")
                        )
                    except ArtifactPipelineError:
                        # A signed failed receipt is a recovered terminal outcome,
                        # not a transport/reconciliation failure.
                        current = await db.get(
                            LabArtifactOperation, operation_row_id
                        )
                        if current is None or current.state not in {
                            "failed", "quarantined"
                        }:
                            raise
                    stats["upload_reconciled"] += 1
                except ArtifactOperationPending:
                    await db.rollback()
                    current = await db.get(LabArtifactOperation, operation_row_id)
                    if current is not None and current.state in {
                        "pending", "processing"
                    }:
                        current.next_retry_at = datetime.now(UTC) + timedelta(
                            seconds=5
                        )
                        await db.commit()
                    stats["upload_pending"] += 1
                except ArtifactOperationNotFound as exc:
                    await db.rollback()
                    current = await db.get(LabArtifactOperation, operation_row_id)
                    if current is None or current.state not in {
                        "pending", "processing"
                    }:
                        continue
                    current_command = _operation_command(
                        UploadLeaseCommand, current
                    )
                    if current_command.expires_at <= datetime.now(UTC):
                        await self.fail_upload_attempt(
                            db,
                            operation=current,
                            error_code="ingest_upload_not_found_after_expiry",
                        )
                    elif current.state == "pending":
                        artifact = await self._lock_artifact(
                            db, current.artifact_id
                        )
                        await self.create_upload_lease(
                            db,
                            artifact=artifact,
                            max_bytes=current_command.max_bytes,
                        )
                    else:
                        current.error_code = str(exc)[:100]
                        current.next_retry_at = datetime.now(UTC) + timedelta(
                            seconds=5
                        )
                        await db.commit()
                    stats["errors"] += 1
                except Exception as exc:  # noqa: BLE001
                    await db.rollback()
                    current = await db.get(LabArtifactOperation, operation_row_id)
                    if current is not None and current.state in {
                        "pending", "processing"
                    }:
                        current.error_code = str(exc)[:100]
                        current.next_retry_at = datetime.now(UTC) + timedelta(
                            seconds=5
                        )
                        await db.commit()
                    stats["errors"] += 1

            pending_scans = (
                await db.execute(
                    select(LabArtifactOperation)
                    .where(
                        LabArtifactOperation.operation_type == "scan",
                        LabArtifactOperation.state == "pending",
                        (
                            LabArtifactOperation.next_retry_at.is_(None)
                            | (
                                LabArtifactOperation.next_retry_at
                                <= datetime.now(UTC)
                            )
                        ),
                    )
                    .order_by(LabArtifactOperation.created_at)
                    .limit(limit)
                )
            ).scalars().all()
            for operation in pending_scans:
                operation_row_id = operation.id
                try:
                    await self.poll_scan(db, operation=operation)
                    stats["scan_polled"] += 1
                except ArtifactOperationNotFound:
                    await db.rollback()
                    current = await db.get(LabArtifactOperation, operation_row_id)
                    if current is None or current.state != "pending":
                        continue
                    command = _operation_command(ScanCommand, current)
                    artifact = await self._lock_artifact(
                        db, current.artifact_id
                    )
                    if command.deadline_at <= datetime.now(UTC):
                        self._record_scan_attempt(
                            artifact=artifact,
                            operation=current,
                            command=command,
                        )
                        current.state = "failed"
                        current.error_code = "scan_job_missing_after_deadline"
                        artifact.scan_status = "failed"
                        artifact.verification_status = "unverified"
                        artifact.scan_error_code = current.error_code
                        current.next_retry_at = (
                            datetime.now(UTC)
                            if artifact.scan_attempts
                            < settings.lab_artifact_scan_max_attempts
                            else None
                        )
                        await db.commit()
                        stats["errors"] += 1
                    else:
                        artifact_id = current.artifact_id
                        await db.rollback()
                        artifact = await db.get(
                            LabArtifact, artifact_id
                        )
                        if artifact is not None:
                            await self.submit_scan(db, artifact=artifact)
                            stats["scan_submitted"] += 1
                except Exception as exc:  # noqa: BLE001
                    await db.rollback()
                    current = await db.get(LabArtifactOperation, operation_row_id)
                    if current is not None and current.state == "pending":
                        current.error_code = str(exc)[:100]
                        current.next_retry_at = datetime.now(UTC) + timedelta(
                            seconds=5
                        )
                        await db.commit()
                    stats["errors"] += 1

            scans = (
                await db.execute(
                    select(LabArtifactOperation)
                    .where(
                        LabArtifactOperation.operation_type == "scan",
                        LabArtifactOperation.state == "processing",
                        (
                            LabArtifactOperation.next_retry_at.is_(None)
                            | (
                                LabArtifactOperation.next_retry_at
                                <= datetime.now(UTC)
                            )
                        ),
                    )
                    .order_by(LabArtifactOperation.created_at)
                    .limit(limit)
                )
            ).scalars().all()
            for operation in scans:
                operation_row_id = operation.id
                try:
                    await self.poll_scan(db, operation=operation)
                    stats["scan_polled"] += 1
                except ArtifactOperationNotFound:
                    await db.rollback()
                    current = await db.get(LabArtifactOperation, operation_row_id)
                    if current is None or current.state != "processing":
                        continue
                    command = _operation_command(ScanCommand, current)
                    if command.deadline_at > datetime.now(UTC):
                        current.next_retry_at = datetime.now(UTC) + timedelta(
                            seconds=5
                        )
                        await db.commit()
                        continue
                    artifact = await self._lock_artifact(
                        db, current.artifact_id
                    )
                    self._record_scan_attempt(
                        artifact=artifact,
                        operation=current,
                        command=command,
                    )
                    current.state = "failed"
                    current.error_code = "scan_job_missing_after_deadline"
                    artifact.scan_status = "failed"
                    artifact.verification_status = "unverified"
                    artifact.scan_error_code = current.error_code
                    current.next_retry_at = (
                        datetime.now(UTC)
                        if artifact.scan_attempts
                        < settings.lab_artifact_scan_max_attempts
                        else None
                    )
                    await db.commit()
                    stats["errors"] += 1
                except Exception as exc:  # noqa: BLE001
                    await db.rollback()
                    current = await db.get(LabArtifactOperation, operation_row_id)
                    if current is not None and current.state == "pending":
                        current.error_code = str(exc)[:100]
                        current.next_retry_at = datetime.now(UTC) + timedelta(seconds=5)
                        await db.commit()
                    stats["errors"] += 1

            deletes = (
                await db.execute(
                    select(LabArtifactOperation)
                    .where(
                        LabArtifactOperation.operation_type == "delete",
                        LabArtifactOperation.state == "pending",
                        (
                            LabArtifactOperation.next_retry_at.is_(None)
                            | (
                                LabArtifactOperation.next_retry_at
                                <= datetime.now(UTC)
                            )
                        ),
                    )
                    .order_by(LabArtifactOperation.created_at)
                    .limit(limit)
                )
            ).scalars().all()
            for operation in deletes:
                operation_row_id = operation.id
                try:
                    await self.submit_delete(db, operation=operation)
                    stats["delete_submitted"] += 1
                except Exception as exc:  # noqa: BLE001
                    await db.rollback()
                    current = await db.get(LabArtifactOperation, operation_row_id)
                    if current is not None and current.state == "pending":
                        current.error_code = str(exc)[:100]
                        current.next_retry_at = datetime.now(UTC) + timedelta(seconds=5)
                        await db.commit()
                    stats["errors"] += 1

            retryable = (
                await db.execute(
                    select(LabArtifactOperation)
                    .where(
                        LabArtifactOperation.operation_type == "delete",
                        LabArtifactOperation.state == "failed",
                        LabArtifactOperation.attempt
                        < settings.lab_artifact_cleanup_max_attempts,
                        LabArtifactOperation.next_retry_at.isnot(None),
                        LabArtifactOperation.next_retry_at <= datetime.now(UTC),
                    )
                    .order_by(LabArtifactOperation.next_retry_at)
                    .limit(limit)
                )
            ).scalars().all()
            for failed in retryable:
                failed_row_id = failed.id
                active = await db.scalar(
                    select(LabArtifactOperation.id).where(
                        LabArtifactOperation.artifact_id == failed.artifact_id,
                        LabArtifactOperation.operation_type == "delete",
                        LabArtifactOperation.state.in_(("pending", "processing")),
                    )
                )
                if active is not None:
                    continue
                artifact = await db.get(LabArtifact, failed.artifact_id)
                if artifact is None:
                    continue
                try:
                    failed_command = _operation_command(DeleteCommand, failed)
                    residue_retry = (
                        failed_command.purpose == "quarantine_residue"
                        and artifact.storage_status == "released"
                        and artifact.quarantine_version_id is not None
                    )
                    if (
                        not residue_retry
                        and artifact.storage_status != "delete_pending"
                    ):
                        continue
                    if residue_retry:
                        await self.stage_quarantine_cleanup(
                            db, artifact=artifact
                        )
                    else:
                        await self.stage_delete(db, artifact=artifact)
                    stats["delete_retried"] += 1
                except Exception as exc:  # noqa: BLE001
                    await db.rollback()
                    failed_row = await db.get(LabArtifactOperation, failed_row_id)
                    if failed_row is not None:
                        failed_row.error_code = str(exc)[:100]
                        failed_row.next_retry_at = datetime.now(UTC) + timedelta(
                            seconds=30
                        )
                        await db.commit()
                    stats["errors"] += 1

            residue_candidates = (
                await db.execute(
                    select(LabArtifact)
                    .where(
                        LabArtifact.storage_status == "released",
                        LabArtifact.quarantine_version_id.isnot(None),
                        LabArtifact.released_version_id.isnot(None),
                    )
                    .order_by(LabArtifact.released_at)
                    .limit(limit)
                )
            ).scalars().all()
            for artifact in residue_candidates:
                active = await db.scalar(
                    select(LabArtifactOperation.id).where(
                        LabArtifactOperation.artifact_id == artifact.id,
                        LabArtifactOperation.operation_type == "delete",
                        LabArtifactOperation.state.in_(("pending", "processing")),
                    )
                )
                if active is not None:
                    continue
                try:
                    await self.stage_quarantine_cleanup(db, artifact=artifact)
                    stats["residue_staged"] += 1
                except Exception:  # noqa: BLE001
                    await db.rollback()
                    stats["errors"] += 1

            retryable_scans = (
                await db.execute(
                    select(LabArtifactOperation)
                    .where(
                        LabArtifactOperation.operation_type == "scan",
                        LabArtifactOperation.state == "failed",
                        LabArtifactOperation.next_retry_at.isnot(None),
                        LabArtifactOperation.next_retry_at <= datetime.now(UTC),
                    )
                    .order_by(LabArtifactOperation.next_retry_at)
                    .limit(limit)
                )
            ).scalars().all()
            for failed in retryable_scans:
                failed_row_id = failed.id
                active = await db.scalar(
                    select(LabArtifactOperation.id).where(
                        LabArtifactOperation.artifact_id == failed.artifact_id,
                        LabArtifactOperation.operation_type == "scan",
                        LabArtifactOperation.state.in_(("pending", "processing")),
                    )
                )
                if active is not None:
                    continue
                artifact = await db.get(LabArtifact, failed.artifact_id)
                if (
                    artifact is None
                    or artifact.storage_status != "quarantined"
                    or artifact.scan_attempts
                    >= settings.lab_artifact_scan_max_attempts
                ):
                    continue
                try:
                    await self.submit_scan(db, artifact=artifact)
                    stats["scan_polled"] += 1
                except Exception as exc:  # noqa: BLE001
                    await db.rollback()
                    failed_row = await db.get(LabArtifactOperation, failed_row_id)
                    if failed_row is not None:
                        failed_row.error_code = str(exc)[:100]
                        failed_row.next_retry_at = datetime.now(UTC) + timedelta(
                            seconds=30
                        )
                        await db.commit()
                    stats["errors"] += 1

            terminal_artifacts = (
                await db.execute(
                    select(LabArtifact).where(
                        LabArtifact.required.is_(True),
                        (
                            (LabArtifact.scan_status == "flagged")
                            | (
                                (LabArtifact.scan_status == "failed")
                                & (
                                    LabArtifact.scan_attempts
                                    >= settings.lab_artifact_scan_max_attempts
                                )
                            )
                        ),
                    )
                    .order_by(LabArtifact.created_at)
                    .limit(limit)
                )
            ).scalars().all()
            for artifact in terminal_artifacts:
                from app.models.lab_run import LabRun
                from app.models.lab_task import LabTask
                from app.services import lab_task_service

                run = await db.scalar(
                    select(LabRun)
                    .where(LabRun.id == artifact.run_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                task = await db.scalar(
                    select(LabTask)
                    .where(LabTask.id == artifact.task_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if (
                    run is None
                    or task is None
                    or run.status != "succeeded"
                    or task.status != "review"
                    or task.accepted_run_id != run.id
                ):
                    continue
                run.status = "failed"
                run.error = f"artifact_{artifact.scan_status}"
                run.ended_at = run.ended_at or datetime.now(UTC)
                try:
                    await lab_task_service.fail_task(
                        db,
                        task,
                        reason=run.error,
                    )
                    await db.commit()
                    stats["terminal_failures"] += 1
                except Exception:  # noqa: BLE001
                    await db.rollback()
                    stats["errors"] += 1
        return stats


async def verify_released_artifact_chain(db, artifact: LabArtifact) -> None:
    """Rebuild and verify the signed upload -> scan -> released locator chain."""
    if artifact.storage_status == "legacy":
        return
    verifier, expected_issuers, _receipt_keys = _load_receipt_trust()
    upload_operation = await db.scalar(
        select(LabArtifactOperation).where(
            LabArtifactOperation.artifact_id == artifact.id,
            LabArtifactOperation.operation_type == "upload",
            LabArtifactOperation.state == "succeeded",
            LabArtifactOperation.receipt_digest == artifact.upload_receipt_digest,
        )
    )
    scan_operation = await db.scalar(
        select(LabArtifactOperation).where(
            LabArtifactOperation.artifact_id == artifact.id,
            LabArtifactOperation.operation_type == "scan",
            LabArtifactOperation.state == "succeeded",
            LabArtifactOperation.receipt_digest == artifact.scan_receipt_digest,
        )
    )
    if upload_operation is None or scan_operation is None:
        raise ArtifactReceiptError("artifact release operations are missing")
    if any(
        operation.epoch != artifact.producer_epoch
        for operation in (upload_operation, scan_operation)
    ):
        raise ArtifactReceiptError("artifact release operation epoch is stale")

    upload_command = _operation_command(UploadLeaseCommand, upload_operation)
    scan_command = _operation_command(ScanCommand, scan_operation)
    upload_receipt = _verify_receipt_binding(
        _stored_receipt(UploadReceipt, upload_operation),
        operation=upload_operation,
        verifier=verifier,
        expected_issuers=expected_issuers,
    )
    scan_receipt = _verify_receipt_binding(
        _stored_receipt(ScanReceipt, scan_operation),
        operation=scan_operation,
        verifier=verifier,
        expected_issuers=expected_issuers,
    )
    if (
        upload_command.upload_id != upload_operation.operation_id
        or scan_command.scan_job_id != scan_operation.operation_id
        or upload_command.tenant_id != artifact.tenant_id
        or scan_command.tenant_id != artifact.tenant_id
        or upload_command.run_id != artifact.run_id
        or scan_command.run_id != artifact.run_id
        or upload_command.session_id != artifact.provider_session_id
        or scan_command.session_id != artifact.provider_session_id
        or upload_command.producer_action_id != artifact.producer_action_id
        or scan_command.producer_action_id != artifact.producer_action_id
        or upload_receipt.status != "completed"
        or upload_receipt.quarantine_ref is None
        or scan_receipt.status != "clean"
        or scan_receipt.released_ref is None
        or upload_receipt.quarantine_ref != scan_command.quarantine_ref
        or scan_receipt.quarantine_ref != scan_command.quarantine_ref
    ):
        raise ArtifactReceiptError("artifact release receipt chain is inconsistent")
    try:
        released_ref = ObjectRef(
            backend=artifact.storage_backend,
            zone="released",
            bucket=artifact.released_bucket,
            key=artifact.released_key,
            version_id=artifact.released_version_id,
            etag=artifact.released_etag,
            byte_size=artifact.byte_size,
            sha256=artifact.sha256,
            content_type=artifact.content_type,
        )
    except Exception as exc:  # noqa: BLE001 - persisted locators are untrusted
        raise ArtifactReceiptError("artifact released locator is invalid") from exc
    if (
        scan_receipt.released_ref != released_ref
        or artifact.sha256 != upload_receipt.sha256
        or artifact.byte_size != upload_receipt.byte_size
        or artifact.content_type != upload_receipt.content_type
        or artifact.scanned_at != scan_receipt.completed_at
        or artifact.released_at != scan_receipt.completed_at
    ):
        raise ArtifactReceiptError("artifact released locator diverges from receipts")


async def run_artifact_reconciler(
    session_factory,
    *,
    client: ArtifactPipelineClient,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        try:
            await client.reconcile_once(session_factory)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a transient DB/service fault must self-heal
            logger.exception("Artifact reconciler iteration failed")
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=settings.lab_artifact_scan_poll_interval_s,
            )
        except TimeoutError:
            pass
