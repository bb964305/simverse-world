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
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

PROTOCOL_VERSION = 1
MAX_EVENT_BYTES = 256 * 1024          # single event cap; over-limit content moves to an artifact reference
MAX_UNACKED_EVENTS = 128              # unacked window
MAX_UNACKED_BYTES = 4 * 1024 * 1024
CANCEL_GRACE_S = 5                    # cooperative cancel
CANCEL_TERM_S = 5                     # wait after TERM before KILL
CANCEL_KILL_S = 10                    # total cancel window; timeout past this -> KILL

EVENT_TYPES: frozenset[str] = frozenset({
    "run.started", "plan.updated", "agent.delegated", "tool.requested",
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
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def args_digest(args: dict) -> str:
    """Stable content hash for a tool call's args, used for idempotency and
    approval-preview integrity checks."""
    return hashlib.sha256(canonical_json(args).encode("utf-8")).hexdigest()


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
