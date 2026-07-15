"""SandboxAdapter — the pluggable interface over a real agent runtime
(OpenClaw / Hermes / computer-use) or the Mock (spec §5.2).

The adapter is deliberately narrow: start an isolated session, submit the goal,
stream steps, answer sensitive-action approvals, collect artifacts, stop. The
runner owns money, persistence, WS, budget/timeout, and redaction — the adapter
only speaks to the runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Protocol, runtime_checkable


@dataclass
class RunSpec:
    """Everything an adapter needs to execute a run. ``secrets`` is always empty
    — no host credentials are injected into the sandbox (spec §5.3)."""
    run_id: str
    task_id: str
    researcher_slug: str
    brief: str
    scopes: list[str]
    budget_usd: float
    deadline: datetime | None = None
    egress_allowlist: list[str] = field(default_factory=list)
    secrets: dict[str, str] = field(default_factory=dict)
    deliverable_kind: str = "report"


@dataclass
class StepEvent:
    """One streamed step. ``approval`` is set (an ``{id, action, summary}`` dict)
    when the step is a sensitive action that must pause for human review."""
    phase: str                       # think | tool_call | observation | message
    summary: str
    tool: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    cost_usd_cents: int = 0
    approval: dict[str, Any] | None = None


@dataclass
class ArtifactSpec:
    kind: str                        # file | link | text | image | dataset
    title: str
    uri: str | None = None
    text_md: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class SandboxHandle:
    """Opaque per-run handle. Concrete adapters subclass or populate this."""


@runtime_checkable
class SandboxAdapter(Protocol):
    name: str

    async def start(self, spec: RunSpec) -> Any: ...
    async def submit_goal(self, handle: Any, brief: str, scopes: list[str]) -> None: ...
    def step_stream(self, handle: Any) -> AsyncIterator[StepEvent]: ...
    async def approve(self, handle: Any, approval_id: str, decision: bool) -> None: ...
    async def collect_artifacts(self, handle: Any) -> list[ArtifactSpec]: ...
    async def stop(self, handle: Any) -> None: ...
