"""Executor service response envelopes and durable receipt signatures."""
from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Mapping

import jwt
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    model_validator,
)

from app.lab.artifact_services.canonical import canonical_digest
from app.lab.artifact_services.mime import declared_mime_matches
from app.lab.artifact_services.schemas import UploadReceipt
from app.lab.protocol import (
    ExecutorArtifactManifest,
    ExecutorJobResult,
    ServiceReceipt,
    content_digest,
)


EXECUTOR_JOB_STATES = frozenset({
    "accepted",
    "starting",
    "running",
    "teardown_pending",
    "succeeded",
    "failed",
    "cancelling",
    "terminating",
    "killing",
    "cancelled",
    "terminated",
    "killed",
    "reconciliation_required",
})
EXECUTOR_TERMINAL_STATES = frozenset({
    "succeeded",
    "failed",
    "cancelled",
    "terminated",
    "killed",
    "reconciliation_required",
})
EXECUTOR_ACTIVE_STATES = EXECUTOR_JOB_STATES - EXECUTOR_TERMINAL_STATES


class _StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExecutorJobStatus(_StrictResponse):
    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    job_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    action_id: str = Field(min_length=1, max_length=200)
    epoch: StrictInt = Field(ge=0)
    state: Literal[
        "accepted",
        "starting",
        "running",
        "teardown_pending",
        "succeeded",
        "failed",
        "cancelling",
        "terminating",
        "killing",
        "cancelled",
        "terminated",
        "killed",
        "reconciliation_required",
    ]
    instance_id: str = Field(min_length=1, max_length=100)
    container_name: str = Field(min_length=1, max_length=128)
    command_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    submit_receipt: ServiceReceipt
    created_at: AwareDatetime
    started_at: AwareDatetime | None = None
    finished_at: AwareDatetime | None = None


class ExecutorJobResultEnvelope(_StrictResponse):
    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    result: ExecutorJobResult
    receipt: ServiceReceipt


class ExecutorArtifactEnvelope(_StrictResponse):
    """Strict successful output evidence embedded in an Executor result."""

    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    manifest: ExecutorArtifactManifest
    upload_receipt: UploadReceipt
    upload_receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_receipt_binding(self) -> "ExecutorArtifactEnvelope":
        receipt = self.upload_receipt
        manifest = self.manifest
        if (
            receipt.status != "completed"
            or receipt.upload_id != manifest.upload_id
            or receipt.artifact_id != manifest.artifact_id
            or receipt.producer_action_id != manifest.producer_action_id
            or receipt.byte_size != manifest.byte_size
            or receipt.sha256 != manifest.sha256
            or not declared_mime_matches(
                manifest.content_type, receipt.content_type or ""
            )
            or canonical_digest(receipt) != self.upload_receipt_digest
        ):
            raise ValueError("executor artifact receipt binding mismatch")
        return self


def deterministic_job_id(action_id: str, epoch: int) -> str:
    if not isinstance(action_id, str) or not action_id:
        raise ValueError("action_id is required")
    if type(epoch) is not int or epoch < 0:
        raise ValueError("epoch must be a non-negative integer")
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"simverse:lab-executor:{action_id}:{epoch}",
        )
    )


class ReceiptValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ReceiptSignerConfig:
    issuer: str
    audience: str
    current_kid: str
    current_key: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value
            for value in (self.issuer, self.audience, self.current_kid)
        ):
            raise ValueError("receipt signer issuer, audience, and kid are required")
        if (
            not isinstance(self.current_key, str)
            or len(self.current_key.encode("utf-8")) < 32
        ):
            raise ValueError("receipt signing key must be at least 32 bytes")


@dataclass(frozen=True)
class ReceiptVerifierConfig:
    issuer: str
    audience: str
    keys: Mapping[str, str]
    leeway_seconds: int = 0

    def __post_init__(self) -> None:
        if not self.issuer or not self.audience:
            raise ValueError("receipt verifier issuer and audience are required")
        if not isinstance(self.keys, Mapping) or not self.keys or any(
            not isinstance(kid, str)
            or not kid
            or not isinstance(key, str)
            or len(key.encode("utf-8")) < 32
            for kid, key in self.keys.items()
        ):
            raise ValueError(
                "receipt verifier key ring must contain named keys of at least 32 bytes"
            )
        if type(self.leeway_seconds) is not int or self.leeway_seconds < 0:
            raise ValueError("receipt verifier leeway must be a non-negative integer")


class ReceiptSigner:
    """Create a canonical receipt whose detached evidence is a compact JWS."""

    def __init__(self, config: ReceiptSignerConfig) -> None:
        self.config = config

    def sign(
        self,
        *,
        operation_id: str,
        request_digest: str,
        epoch: int,
        status: str,
        payload: dict[str, Any],
        run_id: str | None = None,
        session_id: str | None = None,
        action_id: str | None = None,
        artifact_id: str | None = None,
        issued_at: datetime | None = None,
    ) -> ServiceReceipt:
        timestamp = issued_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("receipt issued_at must be timezone-aware")
        payload_digest = content_digest(payload)
        receipt_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                ":".join(
                    (
                        "simverse:executor-receipt",
                        operation_id,
                        request_digest,
                        status,
                        payload_digest,
                    )
                ),
            )
        )
        candidate = {
            "schema_version": 1,
            "receipt_id": receipt_id,
            "issuer": self.config.issuer,
            "kid": self.config.current_kid,
            "operation_id": operation_id,
            "request_digest": request_digest,
            "run_id": run_id,
            "session_id": session_id,
            "action_id": action_id,
            "artifact_id": artifact_id,
            "epoch": epoch,
            "status": status,
            "payload": payload,
            "payload_digest": payload_digest,
            "issued_at": timestamp,
            "signature": "pending",
        }
        draft = ServiceReceipt.model_validate(candidate)
        unsigned = draft.model_dump(mode="json", exclude={"signature"})
        receipt_digest = content_digest(unsigned)
        signature = jwt.encode(
            {
                "iss": self.config.issuer,
                "aud": self.config.audience,
                "receipt_id": receipt_id,
                "receipt_digest": receipt_digest,
                "iat": int(timestamp.timestamp()),
            },
            self.config.current_key,
            algorithm="HS256",
            headers={"kid": self.config.current_kid},
        )
        return ServiceReceipt.model_validate({**unsigned, "signature": signature})


class ReceiptVerifier:
    def __init__(self, config: ReceiptVerifierConfig) -> None:
        self.config = config

    def verify(
        self,
        value: ServiceReceipt | Mapping[str, Any],
        *,
        operation_id: str | None = None,
        request_digest: str | None = None,
        run_id: str | None = None,
        session_id: str | None = None,
        action_id: str | None = None,
        epoch: int | None = None,
        status: str | None = None,
    ) -> ServiceReceipt:
        try:
            receipt = (
                value
                if isinstance(value, ServiceReceipt)
                else ServiceReceipt.model_validate(value)
            )
            header = jwt.get_unverified_header(receipt.signature)
            kid = header.get("kid")
            if header.get("alg") != "HS256" or not isinstance(kid, str):
                raise ReceiptValidationError("untrusted_receipt_key")
            if kid != receipt.kid:
                raise ReceiptValidationError("receipt_kid_mismatch")
            key = self.config.keys.get(kid)
            if not key:
                raise ReceiptValidationError("untrusted_receipt_key")
            claims = jwt.decode(
                receipt.signature,
                key,
                algorithms=["HS256"],
                issuer=self.config.issuer,
                audience=self.config.audience,
                leeway=self.config.leeway_seconds,
                options={
                    "require": [
                        "iss", "aud", "receipt_id", "receipt_digest", "iat"
                    ]
                },
            )
        except ReceiptValidationError:
            raise
        except Exception as exc:
            raise ReceiptValidationError("invalid_receipt_signature") from exc

        unsigned = receipt.model_dump(mode="json", exclude={"signature"})
        expected_receipt_digest = content_digest(unsigned)
        if receipt.issuer != self.config.issuer:
            raise ReceiptValidationError("receipt_issuer_mismatch")
        if not hmac.compare_digest(
            str(claims.get("receipt_digest", "")), expected_receipt_digest
        ) or not hmac.compare_digest(
            str(claims.get("receipt_id", "")), receipt.receipt_id
        ):
            raise ReceiptValidationError("receipt_signature_binding_mismatch")
        if claims.get("iat") != int(receipt.issued_at.timestamp()):
            raise ReceiptValidationError("receipt_issued_at_mismatch")
        expected = {
            "operation_id": operation_id,
            "request_digest": request_digest,
            "run_id": run_id,
            "session_id": session_id,
            "action_id": action_id,
            "epoch": epoch,
            "status": status,
        }
        for field, wanted in expected.items():
            if wanted is not None and getattr(receipt, field) != wanted:
                raise ReceiptValidationError(f"receipt_{field}_mismatch")
        return receipt
