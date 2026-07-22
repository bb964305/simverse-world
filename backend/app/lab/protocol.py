"""Simverse Lab Runtime Protocol v1 — frozen contracts (PRD §Protocols,
§Data and API Evolution, P0 phase).

Defines the wire-level event envelope, the signed capability-grant claim set,
tool descriptors/requests, the approval preview shown to a human reviewer,
and the runtime handshake manifest. This module is contracts + pure helpers
only — no I/O, no execution logic. Policy Engine, Tool Broker, Lease and
Budget enforcement (later tasks) are built on these exact shapes; changing a
field name here is a breaking change for all of them.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

PROTOCOL_V1 = 1
PROTOCOL_V2 = 2
# Existing adapters and Gateway supervision remain protocol-v1 until their P3
# cutover. New v2 schemas below carry their own literal version.
PROTOCOL_VERSION = PROTOCOL_V1
MAX_EVENT_BYTES = 256 * 1024          # single event cap; over-limit content moves to an artifact reference
MAX_COMMAND_BYTES = 256 * 1024
MAX_UNACKED_EVENTS = 128              # unacked window
MAX_UNACKED_BYTES = 4 * 1024 * 1024
CANCEL_GRACE_S = 5                    # cooperative cancel
CANCEL_TERM_S = 5                     # wait after TERM before KILL
CANCEL_KILL_S = 10                    # total cancel window; timeout past this -> KILL

RUNTIME_V2_SAFE_CAPABILITIES: frozenset[str] = frozenset({
    "backpressure",
    "broker_mediation",
    "cancel",
    "checkpoint",
    "control",
    "cursor_replay",
    "events_ack",
    "idempotent_create",
    "kill",
    "reattach",
    "result_receipts",
    "scoped_auth",
    "streaming",
    "subagent",
    "terminate",
})

RUNTIME_V2_SUPERVISION_CAPABILITIES: frozenset[str] = frozenset({
    "backpressure",
    "broker_mediation",
    "cursor_replay",
    "events_ack",
    "idempotent_create",
    "reattach",
    "result_receipts",
    "scoped_auth",
})

EVENT_TYPES: frozenset[str] = frozenset({
    "run.started", "plan.updated", "agent.delegated", "agent.worker_completed",
    "tool.requested",
    "policy.decided", "approval.requested", "approval.resolved",
    "tool.started", "tool.completed", "artifact.emitted",
    "verification.completed", "proposal.drafted", "budget.updated",
    "budget.exhausted", "checkpoint.created", "run.completed", "run.failed",
})


class ProtocolError(Exception):
    """Raised when a runtime handshake or another protocol invariant is violated."""


def canonical_json(obj: Any) -> str:
    """Deterministic JSON encoding: sorted keys (recursively, via sort_keys),
    no whitespace. Used to make hashing/digests independent of dict insertion
    order."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    )


def args_digest(args: dict) -> str:
    """Stable content hash for a tool call's args, used for idempotency and
    approval-preview integrity checks."""
    return hashlib.sha256(canonical_json(args).encode("utf-8")).hexdigest()


def content_digest(value: Any) -> str:
    """SHA-256 of canonical JSON used by protocol-v2 command bindings."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class _StrictV2Model(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _reject_nonfinite_json(cls, value):
        def walk(item: Any) -> None:
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("protocol-v2 JSON cannot contain non-finite numbers")
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ValueError("protocol-v2 JSON object keys must be strings")
                    walk(child)
            elif isinstance(item, (list, tuple)):
                for child in item:
                    walk(child)

        walk(value)
        return value

    @model_validator(mode="after")
    def _check_encoded_size(self):
        size = len(canonical_json(self.model_dump(mode="json")).encode("utf-8"))
        if size > MAX_COMMAND_BYTES:
            raise ValueError(
                f"protocol-v2 object exceeds MAX_COMMAND_BYTES "
                f"({size} > {MAX_COMMAND_BYTES})"
            )
        return self


class RuntimeEvent(_StrictV2Model):
    """Runtime-produced, cursor-addressed protocol-v2 event.

    Runtime owns the provider cursor. Gateway commits this envelope before ACK
    and derives its own canonical ledger sequence independently.
    """

    schema_version: StrictInt = Field(default=2, ge=2, le=2)
    event_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    cursor: StrictInt = Field(ge=1)
    epoch: StrictInt = Field(ge=0)
    event_kind: Literal[
        "session_started",
        "think",
        "tool_intent",
        "tool_result",
        "observation",
        "final",
        "cancelled",
        "failed",
    ]
    turn_id: str | None = Field(default=None, max_length=200)
    intent_id: str | None = Field(default=None, max_length=200)
    outcome: Literal["succeeded", "denied", "failed"] | None = None
    tool_name: str | None = Field(default=None, min_length=1, max_length=200)
    tool_args: dict[str, Any] | None = None
    tool_args_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    payload: dict = Field(default_factory=dict)
    occurred_at: AwareDatetime

    @model_validator(mode="after")
    def _check_event_binding(self) -> "RuntimeEvent":
        if self.event_kind in {"tool_intent", "tool_result", "observation"}:
            if not self.turn_id or not self.intent_id:
                raise ValueError(
                    f"{self.event_kind} requires turn_id and intent_id"
                )
        if self.event_kind == "tool_result" and self.outcome is None:
            raise ValueError("tool_result requires outcome")
        if self.event_kind != "tool_result" and self.outcome is not None:
            raise ValueError("outcome is only valid on tool_result")
        tool_fields = {
            "tool_name": self.tool_name,
            "tool_args": self.tool_args,
            "tool_args_digest": self.tool_args_digest,
        }
        if self.event_kind == "tool_intent":
            missing = [name for name, value in tool_fields.items() if value is None]
            if missing:
                raise ValueError(
                    "tool_intent requires tool_name, tool_args, and "
                    "tool_args_digest"
                )
            if any(name in self.payload for name in tool_fields):
                raise ValueError(
                    "tool_intent binding must use top-level tool_name, tool_args, "
                    "and tool_args_digest"
                )
            if self.tool_args_digest != args_digest(self.tool_args):
                raise ValueError(
                    "tool_args_digest does not match canonical tool_args"
                )
        elif any(value is not None for value in tool_fields.values()):
            raise ValueError(
                "tool_name, tool_args, and tool_args_digest are only valid on "
                "tool_intent"
            )
        return self


class ToolResultCommand(_StrictV2Model):
    """Gateway-produced Broker result that resumes one exact runtime intent."""

    schema_version: StrictInt = Field(default=2, ge=2, le=2)
    command_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    turn_id: str = Field(min_length=1, max_length=200)
    intent_id: str = Field(min_length=1, max_length=200)
    action_id: str = Field(min_length=1, max_length=200)
    outcome: Literal["succeeded", "denied", "failed"]
    payload: dict
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    epoch: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def _check_result_digest(self) -> "ToolResultCommand":
        if self.result_digest != content_digest(self.payload):
            raise ValueError("result_digest does not match canonical payload")
        return self


class ControlCommand(_StrictV2Model):
    """Runner-produced command for one durable Runtime or Executor target.

    Authorization claims belong only to the scoped transport JWT. They must
    never be embedded in this canonical command body.
    """

    schema_version: StrictInt = Field(default=2, ge=2, le=2)
    command_id: str = Field(min_length=1, max_length=200)
    request_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    target_kind: Literal["runtime", "executor"]
    target_id: str = Field(min_length=1, max_length=200)
    action: Literal["cancel", "terminate", "kill"]
    epoch: StrictInt = Field(ge=0)
    deadline_at: AwareDatetime


class RuntimeArtifactManifest(_StrictV2Model):
    """Small Runtime-owned declaration; artifact bytes never cross this API."""

    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    provider_artifact_id: str = Field(min_length=1, max_length=200)
    kind: Literal["file", "link", "text", "image", "dataset"]
    title: str = Field(min_length=1, max_length=200)
    content_type: str = Field(min_length=1, max_length=200)
    original_filename: str | None = Field(default=None, max_length=255)
    declared_byte_size: StrictInt | None = Field(default=None, ge=0)
    expected_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    required: StrictBool = True
    producer_action_id: str | None = Field(default=None, max_length=200)
    upload_state: Literal[
        "pending", "uploading", "uploaded", "acknowledged", "failed"
    ] = "pending"
    upload_receipt: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_production_link(self) -> "RuntimeArtifactManifest":
        if self.kind == "link" and self.required:
            raise ValueError("required runtime artifacts must contain snapshotted bytes")
        if self.upload_state in {"uploaded", "acknowledged", "failed"}:
            if self.upload_receipt is None:
                if self.upload_state != "failed":
                    raise ValueError("uploaded artifact requires upload_receipt")
        elif self.upload_receipt is not None:
            raise ValueError("upload_receipt is only valid after upload")
        return self


class ArtifactUploadLease(_StrictV2Model):
    """One-time, bounded capability used instead of object-store credentials."""

    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    upload_id: str = Field(min_length=1, max_length=200)
    artifact_id: str = Field(min_length=1, max_length=200)
    tenant_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    producer_action_id: str | None = Field(default=None, max_length=200)
    epoch: StrictInt = Field(ge=0)
    upload_url: str = Field(min_length=1, max_length=2048)
    bearer_token: str = Field(min_length=1, max_length=16384)
    max_bytes: StrictInt = Field(gt=0)
    content_type: str = Field(min_length=1, max_length=200)
    expected_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    expires_at: AwareDatetime


class RuntimeArtifactUploadCommand(_StrictV2Model):
    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    command_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    provider_artifact_id: str = Field(min_length=1, max_length=200)
    epoch: StrictInt = Field(ge=0)
    lease: ArtifactUploadLease

    @model_validator(mode="after")
    def _validate_lease_binding(self) -> "RuntimeArtifactUploadCommand":
        if (
            self.run_id != self.lease.run_id
            or self.session_id != self.lease.session_id
            or self.epoch != self.lease.epoch
        ):
            raise ValueError("artifact upload lease binding mismatch")
        return self


class RuntimeArtifactUploadAck(_StrictV2Model):
    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    command_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    provider_artifact_id: str = Field(min_length=1, max_length=200)
    epoch: StrictInt = Field(ge=0)
    upload_receipt_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ExecutorResourceLimits(_StrictV2Model):
    wall_clock_ms: StrictInt = Field(gt=0)
    cpu_millis: StrictInt = Field(gt=0)
    memory_bytes: StrictInt = Field(gt=0)
    pids: StrictInt = Field(gt=0)
    stdout_bytes: StrictInt = Field(gt=0)
    stderr_bytes: StrictInt = Field(gt=0)
    scratch_bytes: StrictInt = Field(gt=0)


class RuntimeExecutorOutputDeclaration(_StrictV2Model):
    """Runtime intent for one file the Executor may export from ``/scratch``."""

    relative_path: str = Field(min_length=1, max_length=500)
    kind: Literal["file", "image", "dataset"]
    expected_use: Literal["deliverable", "evidence"]
    title: str = Field(min_length=1, max_length=200)
    content_type: str = Field(min_length=1, max_length=200)
    original_filename: str | None = Field(default=None, max_length=255)
    required: StrictBool = True
    max_bytes: StrictInt = Field(gt=0)
    expected_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )

    @field_validator("relative_path")
    @classmethod
    def _validate_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            value != value.strip()
            or "\\" in value
            or value.startswith("/")
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or str(path) != value
            or path.parts[0] == ".simverse-executor-exit"
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("executor output path must stay inside scratch")
        return value

    @field_validator("title", "content_type")
    @classmethod
    def _validate_canonical_text(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("executor output text must be canonical")
        return value

    @field_validator("original_filename")
    @classmethod
    def _validate_original_filename(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            value != value.strip()
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or any(ord(char) < 32 for char in value)
        ):
            raise ValueError("executor output filename must be a basename")
        return value


class ExecutorOutputSpec(RuntimeExecutorOutputDeclaration):
    artifact_id: str = Field(min_length=1, max_length=200)
    lease: ArtifactUploadLease


def executor_output_declarations(
    args: dict[str, Any],
) -> list[RuntimeExecutorOutputDeclaration]:
    """Parse the untrusted Runtime ``outputs`` argument as a strict contract."""
    if "outputs" not in args:
        return []
    raw = args["outputs"]
    if not isinstance(raw, list) or len(raw) > 20:
        raise ValueError("executor outputs must be a list of at most 20 declarations")
    declarations = [
        RuntimeExecutorOutputDeclaration.model_validate(item, strict=True)
        for item in raw
    ]
    paths = [declaration.relative_path for declaration in declarations]
    if len(paths) != len(set(paths)):
        raise ValueError("executor output paths must be unique")
    return declarations


class ExecutorArtifactManifest(_StrictV2Model):
    """Actual-byte manifest paired with one terminal Ingest receipt."""

    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    artifact_id: str = Field(min_length=1, max_length=200)
    producer_action_id: str = Field(min_length=1, max_length=200)
    kind: Literal["file", "image", "dataset"]
    expected_use: Literal["deliverable", "evidence"]
    title: str = Field(min_length=1, max_length=200)
    content_type: str = Field(min_length=1, max_length=200)
    original_filename: str | None = Field(default=None, max_length=255)
    required: StrictBool
    byte_size: StrictInt = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    upload_id: str = Field(min_length=1, max_length=200)


class ExecutorJobCommand(_StrictV2Model):
    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    job_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    action_id: str = Field(min_length=1, max_length=200)
    epoch: StrictInt = Field(ge=0)
    tool_name: Literal["code.run", "shell.exec"]
    args: dict[str, Any]
    image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    limits: ExecutorResourceLimits
    outputs: list[ExecutorOutputSpec] = Field(default_factory=list, max_length=20)
    deadline_at: AwareDatetime
    command_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_command_digest(self) -> "ExecutorJobCommand":
        artifact_ids: set[str] = set()
        upload_ids: set[str] = set()
        output_paths: set[str] = set()
        total_output_bytes = 0
        for output in self.outputs:
            lease = output.lease
            if (
                output.artifact_id in artifact_ids
                or output.relative_path in output_paths
                or lease.upload_id in upload_ids
            ):
                raise ValueError("executor outputs must have unique bindings")
            artifact_ids.add(output.artifact_id)
            output_paths.add(output.relative_path)
            upload_ids.add(lease.upload_id)
            if (
                lease.artifact_id != output.artifact_id
                or lease.run_id != self.run_id
                or lease.session_id != self.session_id
                or lease.producer_action_id != self.action_id
                or lease.epoch != self.epoch
                or lease.content_type != output.content_type
                or lease.max_bytes != output.max_bytes
                or lease.expected_sha256 != output.expected_sha256
                or lease.expires_at < self.deadline_at
            ):
                raise ValueError("executor output lease binding mismatch")
            total_output_bytes += output.max_bytes
        if total_output_bytes > self.limits.scratch_bytes:
            raise ValueError("executor output limits exceed scratch capacity")
        canonical = self.model_dump(mode="json", exclude={"command_digest"})
        if self.command_digest != content_digest(canonical):
            raise ValueError("executor command_digest does not match command")
        return self


class ExecutorJobResult(_StrictV2Model):
    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    job_id: str = Field(min_length=1, max_length=200)
    action_id: str = Field(min_length=1, max_length=200)
    epoch: StrictInt = Field(ge=0)
    state: Literal[
        "succeeded",
        "failed",
        "cancelled",
        "terminated",
        "killed",
        "reconciliation_required",
    ]
    exit_code: StrictInt | None = None
    stdout: str = ""
    stderr: str = ""
    artifact_receipts: list[dict[str, Any]] = Field(default_factory=list)
    teardown_proof: dict[str, Any]
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_result_digest(self) -> "ExecutorJobResult":
        canonical = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != content_digest(canonical):
            raise ValueError("executor result_digest does not match result")
        if (
            self.state != "reconciliation_required"
            and self.teardown_proof.get("removed") is not True
        ):
            raise ValueError("terminal executor result requires verified teardown")
        return self


class ServiceReceipt(_StrictV2Model):
    """Canonical signed receipt envelope shared by production trust planes."""

    schema_version: StrictInt = Field(default=1, ge=1, le=1)
    receipt_id: str = Field(min_length=1, max_length=200)
    issuer: str = Field(min_length=1, max_length=100)
    kid: str = Field(min_length=1, max_length=100)
    operation_id: str = Field(min_length=1, max_length=200)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str | None = Field(default=None, max_length=200)
    session_id: str | None = Field(default=None, max_length=200)
    action_id: str | None = Field(default=None, max_length=200)
    artifact_id: str | None = Field(default=None, max_length=200)
    epoch: StrictInt = Field(ge=0)
    status: str = Field(min_length=1, max_length=50)
    payload: dict[str, Any]
    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    issued_at: AwareDatetime
    signature: str = Field(min_length=1, max_length=8192)

    @model_validator(mode="after")
    def _validate_payload_digest(self) -> "ServiceReceipt":
        if self.payload_digest != content_digest(self.payload):
            raise ValueError("receipt payload_digest does not match payload")
        return self


class RuntimeV2Handshake(_StrictV2Model):
    """Provider-owned proof required before a v2 session is registered."""

    schema_version: StrictInt = Field(default=2, ge=2, le=2)
    protocol_version: StrictInt = Field(ge=2, le=2)
    provider_name: str = Field(min_length=1, max_length=80)
    durability_class: Literal["session_affine"]
    reattach_capability: Literal["client_run_id"]
    effect_mode: Literal["broker_only"]
    capabilities: list[str] = Field(min_length=1, max_length=32)

    @field_validator("capabilities")
    @classmethod
    def _validate_capabilities(cls, capabilities: list[str]) -> list[str]:
        if (
            any(
                not capability
                or capability != capability.strip().lower()
                or len(capability) > 80
                for capability in capabilities
            )
            or len(set(capabilities)) != len(capabilities)
        ):
            raise ValueError(
                "runtime capabilities must be canonical, bounded, and unique"
            )
        if "broker_mediation" not in capabilities:
            raise ValueError("runtime missing mandatory broker_mediation capability")
        forbidden = sorted(set(capabilities) - RUNTIME_V2_SAFE_CAPABILITIES)
        if forbidden:
            raise ValueError(
                "runtime advertises forbidden direct effect capability: "
                + ", ".join(forbidden)
            )
        return capabilities


class RuntimeV2SchemaHashes(_StrictV2Model):
    """Hashes for the wire objects whose meaning must match on both peers."""

    runtime_event: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_result_command: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuntimeV2Limits(_StrictV2Model):
    """Flow-control limits proven by the provider before event ingestion."""

    max_event_bytes: StrictInt = Field(gt=0)
    max_command_bytes: StrictInt = Field(gt=0)
    max_unacked_events: StrictInt = Field(gt=0)
    max_unacked_bytes: StrictInt = Field(gt=0)


def runtime_v2_schema_hashes() -> RuntimeV2SchemaHashes:
    """Return canonical hashes of the two result-loop wire schemas."""

    return RuntimeV2SchemaHashes(
        runtime_event=content_digest(RuntimeEvent.model_json_schema()),
        tool_result_command=content_digest(ToolResultCommand.model_json_schema()),
    )


def runtime_v2_protocol_schema_hash() -> str:
    """Bind protocol version and individual wire hashes into one digest."""

    hashes = runtime_v2_schema_hashes()
    return content_digest({
        "protocol_version": PROTOCOL_V2,
        "schema_hashes": hashes.model_dump(mode="json"),
    })


def runtime_v2_limits() -> RuntimeV2Limits:
    return RuntimeV2Limits(
        max_event_bytes=MAX_EVENT_BYTES,
        max_command_bytes=MAX_COMMAND_BYTES,
        max_unacked_events=MAX_UNACKED_EVENTS,
        max_unacked_bytes=MAX_UNACKED_BYTES,
    )


class RuntimeV2SupervisionHandshake(_StrictV2Model):
    """Complete proof required before Gateway emits ``run.started``."""

    manifest: RuntimeV2Handshake
    protocol_schema_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_hashes: RuntimeV2SchemaHashes
    limits: RuntimeV2Limits

    @model_validator(mode="after")
    def _validate_local_contract(self) -> "RuntimeV2SupervisionHandshake":
        missing = sorted(
            RUNTIME_V2_SUPERVISION_CAPABILITIES
            - set(self.manifest.capabilities)
        )
        if missing:
            raise ValueError(
                "runtime missing mandatory supervision capabilities: "
                + ", ".join(missing)
            )
        if self.schema_hashes != runtime_v2_schema_hashes():
            raise ValueError("runtime wire schema hashes do not match Gateway")
        if self.protocol_schema_hash != runtime_v2_protocol_schema_hash():
            raise ValueError("runtime protocol schema hash does not match Gateway")
        if self.limits != runtime_v2_limits():
            raise ValueError("runtime flow-control limits do not match Gateway")
        return self


def runtime_v2_supervision_handshake(
    manifest: RuntimeV2Handshake,
) -> RuntimeV2SupervisionHandshake:
    """Build the local provider proof from the canonical protocol contract."""

    return RuntimeV2SupervisionHandshake(
        manifest=manifest,
        protocol_schema_hash=runtime_v2_protocol_schema_hash(),
        schema_hashes=runtime_v2_schema_hashes(),
        limits=runtime_v2_limits(),
    )


class RunEventEnvelope(BaseModel):
    """One entry in a run's append-only event ledger."""

    schema_version: int = 1
    event_id: str
    tenant_id: str
    run_id: str
    task_id: str
    seq: int = Field(ge=1)
    type: str
    actor: str
    action_id: str | None = None
    parent_id: str | None = None
    fencing_epoch: int = Field(ge=0)
    policy_version: str
    occurred_at: datetime
    trace_id: str | None = None
    payload: dict = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def _validate_type(cls, v: str) -> str:
        if v not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {v!r}")
        return v

    @model_validator(mode="after")
    def _check_payload_size(self) -> "RunEventEnvelope":
        size = len(canonical_json(self.payload).encode("utf-8"))
        if size > MAX_EVENT_BYTES:
            raise ValueError(f"payload exceeds MAX_EVENT_BYTES ({size} > {MAX_EVENT_BYTES})")
        return self


class GrantClaims(BaseModel):
    """Signed capability grant issued to a run's agent (JWT-like claim set).

    depth 0 = the run's top-level agent; depth 1 = a delegated sub-agent.
    Depth > 1 is rejected — v1 allows at most one level of delegation.
    """

    iss: str
    aud: str
    jti: str
    tenant_id: str
    task_id: str
    run_id: str
    agent_id: str
    parent_jti: str | None = None
    depth: int = Field(ge=0, le=1)
    capabilities: list[str]
    resources: dict = Field(default_factory=dict)
    egress: list[str] = []
    budgets: dict[str, int]
    policy_version: str
    fencing_epoch: int
    nbf: int
    exp: int

    @field_validator("aud")
    @classmethod
    def _validate_audience(cls, v: str) -> str:
        if v != "tool-broker":
            raise ValueError('aud must be "tool-broker"')
        return v

    @model_validator(mode="after")
    def _check_exp_after_nbf(self) -> "GrantClaims":
        if self.exp <= self.nbf:
            raise ValueError("exp must be greater than nbf")
        return self


class ToolDescriptor(BaseModel):
    """Static description of a tool the Broker can invoke on the runtime's behalf."""

    name: str
    version: str = "1"
    capability: str
    risk_class: str
    read_only: bool
    side_effect: bool
    timeout_s: int = 60
    max_output_bytes: int = 262144
    concurrency_key: str | None = None

    @field_validator("risk_class")
    @classmethod
    def _validate_risk_class(cls, v: str) -> str:
        if v not in {"R0", "R1", "R2", "R3", "R4"}:
            raise ValueError(f"invalid risk_class: {v!r}")
        return v


class ToolCallRequest(BaseModel):
    """A runtime's intent to call a tool, sent to the Broker for policy evaluation."""

    action_id: str
    run_id: str
    tool_name: str
    tool_version: str = "1"
    args: dict
    args_digest: str
    idempotency_key: str
    deadline_at: datetime | None = None


class ApprovalPreview(BaseModel):
    """Human-reviewable summary of a tool call awaiting approval."""

    action_id: str
    tool_name: str
    target: str
    side_effect: str
    cost_summary: str = ""
    expires_at: datetime
    args_digest: str
    actor: str


class HandshakeManifest(BaseModel):
    """A runtime's capability announcement, exchanged before ``run.started``."""

    protocol_version: int
    runtime: str
    runtime_version: str
    capabilities: list[str]
    supports_checkpoint: bool = False
    supports_resume: bool = False
    cancel_behavior: str = "cooperative"
    event_schema_versions: list[int] = [1]
    max_event_bytes: int = MAX_EVENT_BYTES


def validate_handshake(manifest: HandshakeManifest) -> None:
    """Reject a runtime before ``run.started`` if it can't be trusted to mediate
    every tool effect through the Broker (PRD: fail-closed on mismatch)."""
    if manifest.protocol_version != PROTOCOL_VERSION:
        raise ProtocolError(
            f"unsupported protocol_version {manifest.protocol_version} "
            f"(expected {PROTOCOL_VERSION})"
        )
    if "broker_mediation" not in manifest.capabilities:
        raise ProtocolError("runtime missing mandatory 'broker_mediation' capability")
