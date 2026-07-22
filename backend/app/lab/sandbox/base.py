"""SandboxAdapter — the pluggable interface over a real agent runtime
(OpenClaw / Hermes / computer-use) or the Mock (spec §5.2).

The adapter is deliberately narrow: start an isolated session, submit the goal,
stream steps, answer sensitive-action approvals, collect artifacts, stop. The
runner owns money, persistence, WS, budget/timeout, and redaction — the adapter
only speaks to the runtime.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Protocol, runtime_checkable
import uuid


_MAX_RUNTIME_JSON_DEPTH = 32
_RUNTIME_V2_ARTIFACT_FIELDS = frozenset({
    "artifact_id", "kind", "title", "uri", "text_md", "meta",
})
_RUNTIME_V2_ARTIFACT_KINDS = frozenset({
    "file", "link", "text", "image", "dataset",
})
_RUNTIME_V2_SAFE_FAILURE_DETAILS = {
    "response_byte_cap": "protocol response exceeds byte cap",
    "response_depth_cap": "protocol response exceeds JSON depth cap",
}


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
    provider_artifact_id: str | None = None


@dataclass(frozen=True)
class RuntimeEventBatch:
    """One bounded protocol-v2 replay page from the Runtime."""

    events: list[Any]
    done: bool
    has_more: bool = False
    latest_cursor: int = 0
    acked_through: int = 0


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


class LabAdapterUnavailable(RuntimeError):
    """Raised by ``get_adapter`` when a requested runtime cannot be resolved:
    an unknown name, an empty/implicit name, or a real adapter whose import
    fails. Resolution is fail-closed — a configured runtime must never silently
    fall back to Mock and execute Mock work (recovery plan Phase 1). Only an
    explicit ``mock`` selects Mock."""


class RuntimeV2RequestError(RuntimeError):
    """Classified Runtime request/response failure for orchestration policy."""

    retryable: bool

    def __init__(
        self,
        operation: str,
        *,
        status_code: int | None = None,
        reason_code: str | None = None,
    ) -> None:
        self.operation = operation
        self.status_code = status_code
        if status_code is not None:
            detail = f"HTTP {status_code}"
        elif getattr(self, "retryable", False):
            detail = "transport failure"
        else:
            detail = _RUNTIME_V2_SAFE_FAILURE_DETAILS.get(
                reason_code,
                "protocol response rejected",
            )
        super().__init__(f"Runtime v2 {operation} failed: {detail}")


class RuntimeV2RetryableError(RuntimeV2RequestError):
    retryable = True


class RuntimeV2NonRetryableError(RuntimeV2RequestError):
    retryable = False


class HttpAgentAdapter:
    """Shared skeleton for HTTP-backed real adapters (OpenClaw / Hermes /
    computer-use). Subclasses set ``name`` and read their own base_url/api_key
    from settings. The wire protocol below is a deliberate placeholder to be
    aligned with the concrete runtime during P2 rollout; it is import-safe and
    reuses the process-wide httpx client (trust_env=False).
    """
    name = "http"

    def __init__(
        self,
        base_url: str = "",
        api_key: str = "",
        timeout: float = 60.0,
        *,
        service_token_issuer=None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout
        self._service_token_issuer = service_token_issuer
        self._service_token_config: tuple | None = None
        self._v2_spec: RunSpec | None = None
        self._v2_epoch: int | None = None
        self._v2_client_run_id: str | None = None
        self._v2_provider_session_id: str | None = None
        self._v2_handshake = None

    def _require_configured(self) -> None:
        if not self.base_url:
            raise LabAdapterUnconfigured(f"{self.name} adapter has no base_url configured")

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def prepare_protocol_v2(
        self, *, spec: RunSpec, epoch: int, client_run_id: str
    ) -> None:
        """Bind this adapter instance to one run before any v2 handshake."""
        self._require_configured()
        if type(epoch) is not int or epoch < 0:
            raise LabAdapterUnconfigured("protocol-v2 epoch must be non-negative")
        if not isinstance(client_run_id, str) or not client_run_id:
            raise LabAdapterUnconfigured("protocol-v2 client_run_id is required")
        self._v2_spec = spec
        self._v2_epoch = epoch
        self._v2_client_run_id = client_run_id
        self._v2_provider_session_id = None
        self._v2_handshake = None

    def _require_v2_context(self) -> tuple[RunSpec, int, str]:
        if (
            self._v2_spec is None
            or self._v2_epoch is None
            or self._v2_client_run_id is None
        ):
            raise LabAdapterUnconfigured(
                "protocol-v2 adapter was not bound to a run"
            )
        return self._v2_spec, self._v2_epoch, self._v2_client_run_id

    def _configured_service_token_issuer(self):
        if self._service_token_issuer is not None:
            return self._service_token_issuer
        from app.config import settings
        from app.lab.runtime_ref.service_auth import ServiceTokenIssuer

        values = (
            settings.lab_runtime_auth_issuer,
            settings.lab_runtime_auth_audience,
            settings.lab_runtime_auth_current_kid,
            settings.lab_runtime_auth_current_key,
            settings.lab_runtime_auth_next_kid,
            settings.lab_runtime_auth_next_key,
            settings.lab_runtime_auth_token_ttl_s,
        )
        if (
            not all(isinstance(value, str) and value for value in values[:6])
            or values[1] != "lab-runtime"
            or values[2] == values[4]
            or values[3] == values[5]
        ):
            raise LabAdapterUnconfigured(
                "protocol-v2 Runtime auth requires isolated current/next keys"
            )
        if self._service_token_config != values:
            self._service_token_issuer = ServiceTokenIssuer({
                "issuer": values[0],
                "audience": values[1],
                "current_kid": values[2],
                "current_key": values[3],
                "token_ttl_seconds": values[6],
            })
            self._service_token_config = values
        return self._service_token_issuer

    def _v2_headers(
        self,
        *,
        action: str,
        session_id: str,
        command_id: str | None = None,
    ) -> dict[str, str]:
        spec, epoch, _ = self._require_v2_context()
        token = self._configured_service_token_issuer().issue(
            run_id=spec.run_id,
            session_id=session_id,
            epoch=epoch,
            action=action,
            command_id=command_id,
        )
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _v2_command_id(kind: str, *parts: object) -> str:
        joined = ":".join(str(part) for part in parts)
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"simverse:gateway-runtime:{kind}:{joined}",
            )
        )

    @staticmethod
    def _response_object(
        response, *, max_bytes: int, operation: str = "response.decode"
    ) -> dict:
        if type(max_bytes) is not int or max_bytes <= 0:
            raise ValueError("Runtime response byte cap must be positive")
        if len(response.content) > max_bytes:
            raise RuntimeV2NonRetryableError(
                operation,
                reason_code="response_byte_cap",
            )
        try:
            value = response.json()
        except (ValueError, UnicodeError, RecursionError):
            raise RuntimeV2NonRetryableError(operation) from None
        if not isinstance(value, dict):
            raise RuntimeV2NonRetryableError(operation)
        stack = [(value, 1)]
        while stack:
            item, depth = stack.pop()
            if depth > _MAX_RUNTIME_JSON_DEPTH:
                raise RuntimeV2NonRetryableError(
                    operation,
                    reason_code="response_depth_cap",
                )
            if isinstance(item, dict):
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                stack.extend((child, depth + 1) for child in item)
        return value

    @staticmethod
    async def _request_v2(
        send: Callable[[], Awaitable[Any]], *, operation: str
    ):
        """Send one v2 request and classify only transport/status policy.

        Response bodies are decoded separately under the protocol byte/depth
        limits and are never included in exception text.
        """
        import httpx

        try:
            response = await send()
        except httpx.TransportError as exc:
            raise RuntimeV2RetryableError(operation) from exc

        status_code = getattr(response, "status_code", None)
        if type(status_code) is int and 200 <= status_code < 300:
            return response
        if type(status_code) is int and (
            status_code in {408, 429} or 500 <= status_code < 600
        ):
            raise RuntimeV2RetryableError(
                operation, status_code=status_code
            )
        raise RuntimeV2NonRetryableError(
            operation,
            status_code=status_code if type(status_code) is int else None,
        )

    @staticmethod
    def _artifact_from_v2_wire(value: Any) -> ArtifactSpec:
        if not isinstance(value, dict):
            raise RuntimeV2NonRetryableError("artifacts.read")
        if set(value) != _RUNTIME_V2_ARTIFACT_FIELDS:
            raise RuntimeV2NonRetryableError("artifacts.read")

        artifact_id = value["artifact_id"]
        kind = value["kind"]
        title = value["title"]
        uri = value["uri"]
        text_md = value["text_md"]
        meta = value["meta"]
        if not isinstance(artifact_id, str) or not artifact_id:
            raise RuntimeV2NonRetryableError("artifacts.read")
        if type(kind) is not str or kind not in _RUNTIME_V2_ARTIFACT_KINDS:
            raise RuntimeV2NonRetryableError("artifacts.read")
        if type(title) is not str or not title:
            raise RuntimeV2NonRetryableError("artifacts.read")
        if uri is not None and type(uri) is not str:
            raise RuntimeV2NonRetryableError("artifacts.read")
        if text_md is not None and type(text_md) is not str:
            raise RuntimeV2NonRetryableError("artifacts.read")
        if not (
            isinstance(uri, str)
            and uri.strip()
            or isinstance(text_md, str)
            and text_md.strip()
        ):
            raise RuntimeV2NonRetryableError("artifacts.read")
        if type(meta) is not dict:
            raise RuntimeV2NonRetryableError("artifacts.read")
        return ArtifactSpec(
            kind=kind,
            title=title,
            uri=uri,
            text_md=text_md,
            meta=meta,
            provider_artifact_id=artifact_id,
        )

    @staticmethod
    def _runtime_event_from_wire(value):
        """Normalize only JSON's datetime representation, then validate strictly."""
        from app.lab import protocol

        try:
            if not isinstance(value, dict):
                return protocol.RuntimeEvent.model_validate(value, strict=True)
            candidate = dict(value)
            occurred_at = candidate.get("occurred_at")
            if isinstance(occurred_at, str):
                normalized = (
                    occurred_at[:-1] + "+00:00"
                    if occurred_at.endswith("Z")
                    else occurred_at
                )
                candidate["occurred_at"] = datetime.fromisoformat(normalized)
            return protocol.RuntimeEvent.model_validate(candidate, strict=True)
        except (ValueError, TypeError, RecursionError):
            raise RuntimeV2NonRetryableError("events.read") from None

    async def supervision_handshake(self):
        """Fetch and validate the complete v2 contract before run.started."""
        if self._v2_handshake is not None:
            return self._v2_handshake
        from app.http import get_client
        from app.lab import protocol

        spec, epoch, client_run_id = self._require_v2_context()
        command_id = self._v2_command_id(
            "handshake", spec.run_id, client_run_id, epoch
        )
        response = await self._request_v2(
            lambda: get_client().get(
                f"{self.base_url}/handshake",
                headers=self._v2_headers(
                    action="runtime.handshake",
                    session_id=client_run_id,
                    command_id=command_id,
                ),
                timeout=self.timeout,
            ),
            operation="handshake",
        )
        try:
            self._v2_handshake = protocol.RuntimeV2SupervisionHandshake.model_validate(
                self._response_object(
                    response,
                    max_bytes=protocol.MAX_COMMAND_BYTES,
                    operation="handshake",
                ),
                strict=True,
            )
        except RuntimeV2RequestError:
            raise
        except (ValueError, TypeError, RecursionError):
            raise RuntimeV2NonRetryableError("handshake") from None
        if self._v2_handshake.manifest.provider_name != self.name:
            raise RuntimeV2NonRetryableError("handshake")
        return self._v2_handshake

    async def handshake(self) -> dict:
        """P2 registration consumes the core manifest from the P3 proof."""
        proof = await self.supervision_handshake()
        return proof.manifest.model_dump(mode="json")

    async def _create_or_reattach_v2(
        self, *, client_run_id: str, epoch: int
    ) -> dict:
        from app.http import get_client
        from app.lab.protocol import content_digest

        spec, bound_epoch, bound_client_id = self._require_v2_context()
        if epoch != bound_epoch or client_run_id != bound_client_id:
            raise RuntimeV2NonRetryableError("session.create")
        command_id = self._v2_command_id(
            "session-create", spec.run_id, client_run_id, epoch
        )
        body = {
            "schema_version": 2,
            "command_id": command_id,
            "run_id": spec.run_id,
            "client_run_id": client_run_id,
            "epoch": epoch,
            "scopes": list(spec.scopes),
            "budget_usd": spec.budget_usd,
            "egress_allowlist": list(spec.egress_allowlist),
        }
        response = await self._request_v2(
            lambda: get_client().post(
                f"{self.base_url}/runs",
                headers=self._v2_headers(
                    action="session.create",
                    session_id=client_run_id,
                    command_id=command_id,
                ),
                timeout=self.timeout,
                json=body,
            ),
            operation="session.create",
        )
        from app.lab import protocol

        receipt = self._response_object(
            response,
            max_bytes=protocol.MAX_COMMAND_BYTES,
            operation="session.create",
        )
        provider_session_id = receipt.get("session_id")
        receipt_id = receipt.get("receipt_id")
        if (
            not isinstance(provider_session_id, str)
            or not provider_session_id
            or receipt.get("request_digest") != content_digest(body)
            or not isinstance(receipt_id, str)
            or not receipt_id
            or type(receipt.get("cursor")) is not int
            or receipt["cursor"] < 0
        ):
            raise RuntimeV2NonRetryableError("session.create")
        self._v2_provider_session_id = provider_session_id
        return {
            "locator": {
                "base_url": self.base_url,
                "session_id": provider_session_id,
            },
            "session_id": provider_session_id,
            "durability_class": "session_affine",
        }

    async def create_session(self, *, client_run_id: str, epoch: int) -> dict:
        return await self._create_or_reattach_v2(
            client_run_id=client_run_id, epoch=epoch
        )

    async def reattach_session(self, *, client_run_id: str, epoch: int) -> dict:
        return await self._create_or_reattach_v2(
            client_run_id=client_run_id, epoch=epoch
        )

    async def submit_goal_v2(self, *, provider_session_id: str) -> dict:
        from app.http import get_client
        from app.lab.protocol import content_digest

        spec, epoch, _ = self._require_v2_context()
        command_id = self._v2_command_id(
            "goal", spec.run_id, provider_session_id, epoch
        )
        body = {
            "schema_version": 2,
            "command_id": command_id,
            "run_id": spec.run_id,
            "session_id": provider_session_id,
            "epoch": epoch,
            "brief": spec.brief,
            "scopes": list(spec.scopes),
        }
        response = await self._request_v2(
            lambda: get_client().post(
                f"{self.base_url}/runs/{provider_session_id}/goal",
                headers=self._v2_headers(
                    action="goal.submit",
                    session_id=provider_session_id,
                    command_id=command_id,
                ),
                timeout=self.timeout,
                json=body,
            ),
            operation="goal.submit",
        )
        from app.lab import protocol

        receipt = self._response_object(
            response,
            max_bytes=protocol.MAX_COMMAND_BYTES,
            operation="goal.submit",
        )
        receipt_id = receipt.get("receipt_id")
        turn_id = receipt.get("turn_id")
        if (
            receipt.get("request_digest") != content_digest(body)
            or receipt.get("session_id") != provider_session_id
            or not isinstance(receipt_id, str)
            or not receipt_id
            or not isinstance(turn_id, str)
            or not turn_id
            or receipt.get("state")
            not in {"intent_pending", "completed", "failed"}
            or type(receipt.get("cursor")) is not int
            or receipt["cursor"] < 0
        ):
            raise RuntimeV2NonRetryableError("goal.submit")
        return receipt

    async def read_runtime_events(
        self,
        *,
        provider_session_id: str,
        after: int,
        limit: int,
        max_bytes: int,
    ) -> RuntimeEventBatch:
        from app.http import get_client
        from app.lab import protocol

        spec, epoch, _ = self._require_v2_context()
        if (
            type(after) is not int
            or after < 0
            or type(limit) is not int
            or not 1 <= limit <= protocol.MAX_UNACKED_EVENTS
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= protocol.MAX_UNACKED_BYTES
        ):
            raise ValueError("invalid Runtime event read window")
        response = await self._request_v2(
            lambda: get_client().get(
                f"{self.base_url}/runs/{provider_session_id}/events",
                headers=self._v2_headers(
                    action="events.read", session_id=provider_session_id
                ),
                timeout=self.timeout,
                params={"after": after, "limit": limit, "max_bytes": max_bytes},
            ),
            operation="events.read",
        )
        body = self._response_object(
            response,
            max_bytes=max_bytes + protocol.MAX_COMMAND_BYTES,
            operation="events.read",
        )
        raw_events = body.get("events")
        if (
            not isinstance(raw_events, list)
            or type(body.get("done")) is not bool
            or type(body.get("has_more")) is not bool
            or type(body.get("latest_cursor")) is not int
            or type(body.get("acked_through")) is not int
        ):
            raise RuntimeV2NonRetryableError("events.read")
        if len(raw_events) > limit:
            raise RuntimeV2NonRetryableError("events.read")
        events = [self._runtime_event_from_wire(value) for value in raw_events]
        event_sizes = [
            len(protocol.canonical_json(value.model_dump(mode="json")).encode("utf-8"))
            for value in events
        ]
        if any(size > protocol.MAX_EVENT_BYTES for size in event_sizes):
            raise RuntimeV2NonRetryableError("events.read")
        encoded_bytes = sum(event_sizes)
        if encoded_bytes > max_bytes:
            raise RuntimeV2NonRetryableError("events.read")
        prior = after
        for event in events:
            if (
                event.run_id != spec.run_id
                or event.session_id != provider_session_id
                or event.epoch != epoch
                or event.cursor != prior + 1
            ):
                raise RuntimeV2NonRetryableError("events.read")
            prior = event.cursor
        if (
            body["acked_through"] != after
            or body["acked_through"] > body["latest_cursor"]
            or (events and events[-1].cursor > body["latest_cursor"])
            or body["has_more"] != (prior < body["latest_cursor"])
            or (body["done"] and body["has_more"])
        ):
            raise RuntimeV2NonRetryableError("events.read")
        return RuntimeEventBatch(
            events=events,
            done=body["done"],
            has_more=body["has_more"],
            latest_cursor=body["latest_cursor"],
            acked_through=body["acked_through"],
        )

    async def ack_runtime_events(
        self, *, provider_session_id: str, cursor: int
    ) -> dict:
        from app.http import get_client
        from app.lab.protocol import content_digest

        spec, epoch, _ = self._require_v2_context()
        command_id = self._v2_command_id(
            "events-ack", spec.run_id, provider_session_id, epoch, cursor
        )
        body = {
            "schema_version": 2,
            "command_id": command_id,
            "run_id": spec.run_id,
            "session_id": provider_session_id,
            "epoch": epoch,
            "cursor": cursor,
        }
        response = await self._request_v2(
            lambda: get_client().post(
                f"{self.base_url}/runs/{provider_session_id}/events/ack",
                headers=self._v2_headers(
                    action="events.ack",
                    session_id=provider_session_id,
                    command_id=command_id,
                ),
                timeout=self.timeout,
                json=body,
            ),
            operation="events.ack",
        )
        from app.lab import protocol

        receipt = self._response_object(
            response,
            max_bytes=protocol.MAX_COMMAND_BYTES,
            operation="events.ack",
        )
        receipt_id = receipt.get("receipt_id")
        if (
            receipt.get("request_digest") != content_digest(body)
            or receipt.get("session_id") != provider_session_id
            or receipt.get("acked_through") != cursor
            or not isinstance(receipt_id, str)
            or not receipt_id
        ):
            raise RuntimeV2NonRetryableError("events.ack")
        return receipt

    async def send_runtime_result(self, command) -> dict:
        from app.http import get_client

        spec, epoch, _ = self._require_v2_context()
        if command.run_id != spec.run_id or command.epoch != epoch:
            raise RuntimeV2NonRetryableError("tool_result.submit")
        response = await self._request_v2(
            lambda: get_client().post(
                f"{self.base_url}/runs/{command.session_id}/results",
                headers=self._v2_headers(
                    action="tool_result.submit",
                    session_id=command.session_id,
                    command_id=command.command_id,
                ),
                timeout=self.timeout,
                json=command.model_dump(mode="json"),
            ),
            operation="tool_result.submit",
        )
        from app.lab import protocol

        receipt = self._response_object(
            response,
            max_bytes=protocol.MAX_COMMAND_BYTES,
            operation="tool_result.submit",
        )
        expected_digest = protocol.content_digest(command.model_dump(mode="json"))
        expected_fields = {
            "session_id": command.session_id,
            "turn_id": command.turn_id,
            "intent_id": command.intent_id,
            "action_id": command.action_id,
        }
        if (
            not isinstance(receipt.get("receipt_id"), str)
            or not receipt["receipt_id"]
            or receipt.get("request_digest") != expected_digest
            or receipt.get("state") != "runtime_acked"
            or any(
                receipt.get(name) != value
                for name, value in expected_fields.items()
            )
        ):
            raise RuntimeV2NonRetryableError("tool_result.submit")
        return receipt

    async def control_runtime_v2(self, command) -> dict:
        """Send one canonical, receipt-bearing Runtime control command."""
        from app.http import get_client
        from app.lab import protocol

        self._require_configured()
        try:
            body = protocol.ControlCommand.model_validate(command)
        except (TypeError, ValueError):
            raise RuntimeV2NonRetryableError("runtime.control") from None
        if body.target_kind != "runtime" or body.target_id != body.session_id:
            raise RuntimeV2NonRetryableError("runtime.control")

        token = self._configured_service_token_issuer().issue(
            run_id=body.run_id,
            session_id=body.session_id,
            epoch=body.epoch,
            action="runtime.control",
            command_id=body.command_id,
        )
        response = await self._request_v2(
            lambda: get_client().post(
                f"{self.base_url}/runs/{body.session_id}/{body.action}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
                json=body.model_dump(mode="json"),
            ),
            operation="runtime.control",
        )
        receipt = self._response_object(
            response,
            max_bytes=protocol.MAX_COMMAND_BYTES,
            operation="runtime.control",
        )
        expected = {
            "request_digest": protocol.content_digest(
                body.model_dump(mode="json")
            ),
            "request_id": body.request_id,
            "run_id": body.run_id,
            "session_id": body.session_id,
            "target_id": body.target_id,
            "action": body.action,
            "epoch": body.epoch,
            "status": "confirmed_stopped",
        }
        if (
            not isinstance(receipt.get("receipt_id"), str)
            or not receipt["receipt_id"]
            or any(receipt.get(name) != value for name, value in expected.items())
            or receipt.get("runtime_state")
            not in {"completed", "failed", "cancelled"}
        ):
            raise RuntimeV2NonRetryableError("runtime.control")
        return receipt

    async def collect_artifacts_v2(
        self, *, provider_session_id: str
    ) -> list[ArtifactSpec]:
        from app.http import get_client
        from app.config import settings
        from app.lab import protocol

        response = await self._request_v2(
            lambda: get_client().get(
                f"{self.base_url}/runs/{provider_session_id}/artifacts",
                headers=self._v2_headers(
                    action="artifacts.read", session_id=provider_session_id
                ),
                timeout=self.timeout,
            ),
            operation="artifacts.read",
        )
        body = self._response_object(
            response,
            max_bytes=max(1, int(settings.lab_budget_artifact_bytes))
            + protocol.MAX_COMMAND_BYTES,
            operation="artifacts.read",
        )
        if set(body) != {"artifacts"}:
            raise RuntimeV2NonRetryableError("artifacts.read")
        raw = body["artifacts"]
        if not isinstance(raw, list) or not raw:
            raise RuntimeV2NonRetryableError("artifacts.read")
        return [self._artifact_from_v2_wire(value) for value in raw]

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
