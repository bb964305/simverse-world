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
    when the step is a sensitive action that must pause for human review.

    ``model_tokens`` is the LLM token count this step burned; the orchestrator
    debits it against the run's ``model_tokens`` budget. It defaults to 0 so a
    legacy adapter that never reports tokens simply spends nothing on that
    dimension (no behaviour change)."""
    phase: str                       # think | tool_call | observation | message
    summary: str
    tool: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    cost_usd_cents: int = 0
    model_tokens: int = 0
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

    # Optional cancel-escalation surface used by ``app.lab.supervision`` (P2-D).
    # An adapter that omits these is treated as already-stopped / healthy — the
    # supervisor probes them via ``getattr`` so legacy adapters keep working
    # unchanged; the Mock and HttpAgentAdapter below give minimal implementations.
    async def cancel(self, handle: Any) -> None: ...        # cooperative cancel
    async def terminate(self, handle: Any) -> None: ...     # TERM escalation
    async def kill(self, handle: Any) -> None: ...          # KILL escalation
    async def health(self, handle: Any) -> dict: ...        # {"alive": bool, "cancelled": bool}


class LabAdapterUnconfigured(RuntimeError):
    """Raised at start() when a real adapter has no ``base_url`` configured.
    Never raised at import — the module always imports so the app boots without
    the external runtime (spec §13 P2: 空串=未配置, 无外部依赖也必须能 import)."""


class HttpAgentAdapter:
    """Shared skeleton for HTTP-backed real adapters (OpenClaw / Hermes /
    computer-use). Subclasses set ``name`` and read their own base_url/api_key
    from settings. The wire protocol below is a deliberate placeholder to be
    aligned with the concrete runtime during P2 rollout; it is import-safe and
    reuses the process-wide httpx client (trust_env=False).
    """
    name = "http"

    def __init__(self, base_url: str = "", api_key: str = "", timeout: float = 60.0) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout

    def _require_configured(self) -> None:
        if not self.base_url:
            raise LabAdapterUnconfigured(f"{self.name} adapter has no base_url configured")

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    async def start(self, spec: RunSpec) -> "HttpHandle":
        self._require_configured()
        from app.http import get_client
        resp = await get_client().post(
            f"{self.base_url}/runs", headers=self._headers(), timeout=self.timeout,
            json={"run_id": spec.run_id, "scopes": spec.scopes, "budget_usd": spec.budget_usd,
                  "egress_allowlist": spec.egress_allowlist},
        )
        resp.raise_for_status()
        session_id = (resp.json() or {}).get("session_id", spec.run_id)
        return HttpHandle(self, session_id, spec)

    async def submit_goal(self, handle: "HttpHandle", brief: str, scopes: list[str]) -> None:
        from app.http import get_client
        resp = await get_client().post(
            f"{self.base_url}/runs/{handle.session_id}/goal", headers=self._headers(),
            timeout=self.timeout, json={"brief": brief, "scopes": scopes},
        )
        resp.raise_for_status()

    async def step_stream(self, handle: "HttpHandle") -> AsyncIterator[StepEvent]:
        from app.http import get_client
        after = 0
        while True:
            resp = await get_client().get(
                f"{self.base_url}/runs/{handle.session_id}/steps", headers=self._headers(),
                timeout=self.timeout, params={"after": after},
            )
            resp.raise_for_status()
            data = resp.json() or {}
            for raw in data.get("steps", []):
                after = max(after, int(raw.get("seq", after)))
                yield StepEvent(
                    phase=raw.get("phase", "message"), summary=raw.get("summary", ""),
                    tool=raw.get("tool"), payload=raw.get("payload") or {},
                    cost_usd_cents=int(raw.get("cost_usd_cents", 0)),
                    model_tokens=int(raw.get("model_tokens", 0)),
                    approval=raw.get("approval"),
                )
            if data.get("done"):
                break

    async def read_provider_events(self, handle: "HttpHandle") -> AsyncIterator[tuple[int, StepEvent]]:
        """Like ``step_stream`` but surfaces the runtime's own monotonic step
        ``seq`` as the *provider cursor* alongside each event, so a Gateway
        supervisor can drive ``supervision.ingest_provider_event`` (dedup / ACK /
        replay). The provider cursor is the runtime-side polling ``after`` value —
        deliberately DISTINCT from the ledger's durable ``seq`` (P2-D/P2-F). This
        makes a real HTTP adapter supervisable; the Mock path is untouched and does
        not flow through supervision."""
        from app.http import get_client
        after = 0
        while True:
            resp = await get_client().get(
                f"{self.base_url}/runs/{handle.session_id}/steps", headers=self._headers(),
                timeout=self.timeout, params={"after": after},
            )
            resp.raise_for_status()
            data = resp.json() or {}
            for raw in data.get("steps", []):
                cursor = int(raw.get("seq", after))
                after = max(after, cursor)
                yield cursor, StepEvent(
                    phase=raw.get("phase", "message"), summary=raw.get("summary", ""),
                    tool=raw.get("tool"), payload=raw.get("payload") or {},
                    cost_usd_cents=int(raw.get("cost_usd_cents", 0)),
                    model_tokens=int(raw.get("model_tokens", 0)),
                    approval=raw.get("approval"),
                )
            if data.get("done"):
                break

    async def approve(self, handle: "HttpHandle", approval_id: str, decision: bool) -> None:
        from app.http import get_client
        resp = await get_client().post(
            f"{self.base_url}/runs/{handle.session_id}/approve", headers=self._headers(),
            timeout=self.timeout, json={"approval_id": approval_id, "decision": decision},
        )
        resp.raise_for_status()

    async def collect_artifacts(self, handle: "HttpHandle") -> list[ArtifactSpec]:
        from app.http import get_client
        resp = await get_client().get(
            f"{self.base_url}/runs/{handle.session_id}/artifacts", headers=self._headers(),
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return [
            ArtifactSpec(kind=a.get("kind", "text"), title=a.get("title", ""), uri=a.get("uri"),
                         text_md=a.get("text_md"), meta=a.get("meta") or {})
            for a in (resp.json() or {}).get("artifacts", [])
        ]

    async def stop(self, handle: "HttpHandle") -> None:
        from app.http import get_client
        try:
            await get_client().post(
                f"{self.base_url}/runs/{handle.session_id}/stop", headers=self._headers(),
                timeout=self.timeout,
            )
        except Exception:
            pass  # best-effort teardown

    async def _post_control(self, handle: "HttpHandle", action: str) -> None:
        """Best-effort control POST (cancel/terminate/kill). Placeholder wire
        protocol aligned with the P2 rollout — import-safe, never raises so a
        supervisor escalation always proceeds to the next tier / to fencing."""
        from app.http import get_client
        try:
            await get_client().post(
                f"{self.base_url}/runs/{handle.session_id}/{action}", headers=self._headers(),
                timeout=self.timeout,
            )
        except Exception:
            pass

    async def cancel(self, handle: "HttpHandle") -> None:
        await self._post_control(handle, "cancel")

    async def terminate(self, handle: "HttpHandle") -> None:
        await self._post_control(handle, "terminate")

    async def kill(self, handle: "HttpHandle") -> None:
        await self._post_control(handle, "kill")

    async def health(self, handle: "HttpHandle") -> dict:
        """Runtime liveness for the supervisor's cancel poll. Unreachable →
        report stopped (fail-closed: the supervisor stops waiting and fences)."""
        from app.http import get_client
        try:
            resp = await get_client().get(
                f"{self.base_url}/runs/{handle.session_id}/health", headers=self._headers(),
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json() or {}
            return {"alive": bool(data.get("alive", False)),
                    "cancelled": bool(data.get("cancelled", False))}
        except Exception:
            return {"alive": False, "cancelled": True}


class HttpHandle(SandboxHandle):
    def __init__(self, adapter: HttpAgentAdapter, session_id: str, spec: RunSpec) -> None:
        self.adapter = adapter
        self.session_id = session_id
        self.spec = spec
