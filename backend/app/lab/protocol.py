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
from typing import Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
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
    "kill",
    "reattach",
    "streaming",
    "subagent",
    "terminate",
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
