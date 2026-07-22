"""Strict wire schemas for the independent Artifact services."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from app.lab.artifact_services.canonical import canonical_digest


Sha256 = str
ReceiptStatus = Literal[
    "leased", "completed", "pending", "running", "clean", "flagged", "failed"
]


def _parse_aware_datetime(value):
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            value = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("datetime must be RFC3339/ISO8601") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


StrictAwareDatetime = Annotated[AwareDatetime, BeforeValidator(_parse_aware_datetime)]


class StrictWireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _canonical_identifier(value: str) -> str:
    if value != value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError("identifier must be canonical printable text")
    return value


class ObjectRef(StrictWireModel):
    """An opaque, exact object version.  No operation may omit version_id."""

    backend: Literal["filesystem", "s3"]
    zone: Literal["quarantine", "released"]
    bucket: str = Field(min_length=1, max_length=128)
    key: str = Field(min_length=1, max_length=1024)
    version_id: str = Field(min_length=1, max_length=512)
    etag: str = Field(min_length=1, max_length=256)
    byte_size: StrictInt = Field(ge=0)
    sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str = Field(min_length=1, max_length=200)

    @field_validator("bucket", "key", "version_id", "etag", "content_type")
    @classmethod
    def canonical_text(cls, value: str) -> str:
        return _canonical_identifier(value)


class CommandBase(StrictWireModel):
    schema_version: Literal[1] = 1
    command_id: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    artifact_id: str = Field(min_length=1, max_length=200)
    producer_action_id: str | None = Field(default=None, min_length=1, max_length=200)
    epoch: StrictInt = Field(ge=0)

    @field_validator(
        "command_id", "tenant_id", "run_id", "session_id", "artifact_id",
        "producer_action_id",
    )
    @classmethod
    def canonical_ids(cls, value: str | None) -> str | None:
        return None if value is None else _canonical_identifier(value)


class UploadLeaseCommand(CommandBase):
    upload_id: str = Field(min_length=1, max_length=200)
    content_type: str = Field(min_length=1, max_length=200)
    max_bytes: StrictInt = Field(gt=0)
    expected_sha256: Sha256 | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    declared_byte_size: StrictInt | None = Field(default=None, ge=0)
    expires_at: StrictAwareDatetime

    @field_validator("upload_id", "content_type")
    @classmethod
    def canonical_upload_text(cls, value: str) -> str:
        return _canonical_identifier(value)

    @model_validator(mode="after")
    def declared_size_within_lease(self) -> "UploadLeaseCommand":
        if self.declared_byte_size is not None and self.declared_byte_size > self.max_bytes:
            raise ValueError("declared_byte_size exceeds max_bytes")
        return self


class ScanCommand(CommandBase):
    scan_job_id: str = Field(min_length=1, max_length=200)
    quarantine_ref: ObjectRef
    sha256: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: StrictInt = Field(ge=0)
    content_type: str = Field(min_length=1, max_length=200)
    policy_version: str = Field(min_length=1, max_length=100)
    deadline_at: StrictAwareDatetime

    @field_validator("scan_job_id", "content_type", "policy_version")
    @classmethod
    def canonical_scan_text(cls, value: str) -> str:
        return _canonical_identifier(value)

    @model_validator(mode="after")
    def exact_quarantine_binding(self) -> "ScanCommand":
        if self.quarantine_ref.zone != "quarantine":
            raise ValueError("scan command must bind an exact quarantine version")
        if (
            self.quarantine_ref.sha256 != self.sha256
            or self.quarantine_ref.byte_size != self.byte_size
        ):
            raise ValueError("scan command digest/size diverges from object reference")
        return self


class DeleteTarget(StrictWireModel):
    object_ref: ObjectRef


class DeleteCommand(CommandBase):
    delete_operation_id: str = Field(min_length=1, max_length=200)
    purpose: Literal["retention_expiry", "quarantine_residue"]
    targets: list[DeleteTarget] = Field(min_length=1, max_length=16)
    deadline_at: StrictAwareDatetime

    @field_validator("delete_operation_id")
    @classmethod
    def canonical_delete_id(cls, value: str) -> str:
        return _canonical_identifier(value)

    @model_validator(mode="after")
    def unique_exact_targets(self) -> "DeleteCommand":
        identities = [
            (
                target.object_ref.backend,
                target.object_ref.bucket,
                target.object_ref.key,
                target.object_ref.version_id,
            )
            for target in self.targets
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("delete targets must be unique exact versions")
        return self


class ReceiptBase(StrictWireModel):
    schema_version: Literal[1] = 1
    algorithm: Literal["HS256", "EdDSA"]
    receipt_type: str = Field(min_length=1, max_length=80)
    receipt_id: str = Field(min_length=1, max_length=200)
    issuer: str = Field(min_length=1, max_length=200)
    kid: str = Field(min_length=1, max_length=200)
    service_instance_id: str = Field(min_length=1, max_length=200)
    command_id: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=100)
    request_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    tenant_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    artifact_id: str = Field(min_length=1, max_length=200)
    producer_action_id: str | None = Field(default=None, min_length=1, max_length=200)
    epoch: StrictInt = Field(ge=0)
    status: ReceiptStatus
    occurred_at: StrictAwareDatetime
    payload_digest: Sha256 = Field(pattern=r"^[0-9a-f]{64}$")
    signature: str = Field(pattern=r"^(?:[A-Za-z0-9_-]{43}|[A-Za-z0-9_-]{86})$")

    @field_validator(
        "receipt_type", "receipt_id", "issuer", "kid", "service_instance_id",
        "command_id", "action", "tenant_id", "run_id", "session_id", "artifact_id",
        "producer_action_id",
    )
    @classmethod
    def canonical_receipt_text(cls, value: str | None) -> str | None:
        return None if value is None else _canonical_identifier(value)


class UploadLeaseReceipt(ReceiptBase):
    receipt_type: Literal["artifact.upload_lease"] = "artifact.upload_lease"
    status: Literal["leased"] = "leased"
    upload_id: str = Field(min_length=1, max_length=200)
    expires_at: StrictAwareDatetime
    max_bytes: StrictInt = Field(gt=0)
    upload_token: str = Field(min_length=1, max_length=16 * 1024)

    @model_validator(mode="after")
    def lease_payload_digest_is_exact(self) -> "UploadLeaseReceipt":
        outcome = {
            "upload_id": self.upload_id,
            "expires_at": self.expires_at.isoformat(),
            "max_bytes": self.max_bytes,
        }
        if self.payload_digest != canonical_digest(outcome):
            raise ValueError("upload lease payload_digest does not match payload")
        return self


class UploadReceipt(ReceiptBase):
    receipt_type: Literal["artifact.upload"] = "artifact.upload"
    status: Literal["completed", "failed"]
    upload_id: str = Field(min_length=1, max_length=200)
    quarantine_ref: ObjectRef | None = None
    byte_size: StrictInt | None = Field(default=None, ge=0)
    sha256: Sha256 | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    content_type: str | None = Field(default=None, min_length=1, max_length=200)
    completed_at: StrictAwareDatetime
    error_code: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def completed_payload_is_exact(self) -> "UploadReceipt":
        if self.status == "completed":
            if any(
                value is None
                for value in (
                    self.quarantine_ref, self.byte_size, self.sha256, self.content_type
                )
            ) or self.error_code is not None:
                raise ValueError("completed upload receipt is incomplete")
        elif self.error_code is None:
            raise ValueError("failed upload receipt requires error_code")
        if self.quarantine_ref is None:
            if any(
                value is not None
                for value in (self.byte_size, self.sha256, self.content_type)
            ):
                raise ValueError("upload receipt object metadata requires an exact reference")
        else:
            if self.quarantine_ref.zone != "quarantine":
                raise ValueError("upload receipt must bind a quarantine version")
            if any(
                value is None
                for value in (self.byte_size, self.sha256, self.content_type)
            ):
                raise ValueError("upload receipt object metadata is incomplete")
            if (
                self.quarantine_ref.byte_size != self.byte_size
                or self.quarantine_ref.sha256 != self.sha256
                or self.quarantine_ref.content_type != self.content_type
            ):
                raise ValueError("upload receipt metadata diverges from object reference")
        outcome = {
            "status": self.status,
            "quarantine_ref": (
                None
                if self.quarantine_ref is None
                else self.quarantine_ref.model_dump(mode="json")
            ),
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "content_type": self.content_type,
        }
        if self.status == "failed":
            outcome["error_code"] = self.error_code
        if self.payload_digest != canonical_digest(outcome):
            raise ValueError("upload payload_digest does not match payload")
        return self


class ScanReceipt(ReceiptBase):
    receipt_type: Literal["artifact.scan"] = "artifact.scan"
    status: Literal["pending", "running", "clean", "flagged", "failed"]
    scan_job_id: str = Field(min_length=1, max_length=200)
    policy_version: str = Field(min_length=1, max_length=100)
    quarantine_ref: ObjectRef
    released_ref: ObjectRef | None = None
    scan_engine_version: str | None = Field(default=None, min_length=1, max_length=200)
    completed_at: StrictAwareDatetime | None = None
    error_code: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def scan_result_shape(self) -> "ScanReceipt":
        if self.quarantine_ref.zone != "quarantine":
            raise ValueError("scan receipt must bind a quarantine version")
        if self.status == "clean":
            if self.released_ref is None or self.released_ref.zone != "released":
                raise ValueError("clean receipt requires an exact released version")
            if (
                self.released_ref.sha256 != self.quarantine_ref.sha256
                or self.released_ref.byte_size != self.quarantine_ref.byte_size
                or self.released_ref.content_type != self.quarantine_ref.content_type
            ):
                raise ValueError("released object diverges from scanned quarantine object")
            if self.completed_at is None or self.error_code is not None:
                raise ValueError("clean receipt terminal fields are invalid")
        elif self.status in {"flagged", "failed"}:
            if self.released_ref is not None or self.completed_at is None or not self.error_code:
                raise ValueError("non-clean terminal scan receipt is invalid")
        elif (
            self.completed_at is not None
            or self.released_ref is not None
            or self.error_code is not None
        ):
            raise ValueError("non-terminal scan receipt cannot carry release state")
        if self.scan_engine_version is None:
            raise ValueError("scan receipt requires an engine version")
        outcome = {
            "status": self.status,
            "quarantine_ref": self.quarantine_ref.model_dump(mode="json"),
            "released_ref": (
                None
                if self.released_ref is None
                else self.released_ref.model_dump(mode="json")
            ),
            "policy_version": self.policy_version,
            "engine_version": self.scan_engine_version,
            "error_code": self.error_code,
        }
        if self.payload_digest != canonical_digest(outcome):
            raise ValueError("scan payload_digest does not match payload")
        return self


class DeleteProof(StrictWireModel):
    object_ref: ObjectRef
    absent: Literal[True] = True
    checked_at: StrictAwareDatetime


class DeleteReceipt(ReceiptBase):
    receipt_type: Literal["artifact.delete"] = "artifact.delete"
    status: Literal["completed", "failed"]
    delete_operation_id: str = Field(min_length=1, max_length=200)
    proofs: list[DeleteProof] = Field(default_factory=list, max_length=16)
    completed_at: StrictAwareDatetime
    error_code: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def delete_result_shape(self) -> "DeleteReceipt":
        if self.status == "completed" and self.error_code is not None:
            raise ValueError("completed delete receipt cannot contain an error")
        if self.status == "failed" and not self.error_code:
            raise ValueError("failed delete receipt requires an error")
        outcome = {
            "status": self.status,
            "proofs": [proof.model_dump(mode="json") for proof in self.proofs],
            "error_code": self.error_code,
        }
        if self.payload_digest != canonical_digest(outcome):
            raise ValueError("delete payload_digest does not match payload")
        return self


def receipt_signing_payload(receipt: ReceiptBase | dict) -> dict:
    value = (
        receipt.model_dump(mode="json")
        if isinstance(receipt, ReceiptBase)
        else dict(receipt)
    )
    value.pop("signature", None)
    return value


def utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.isoformat()
