"""Simverse reference runtime — standalone HTTP server (recovery plan Phase 7).

A real, self-hosted agent runtime speaking the Lab HTTP wire protocol
(``HttpAgentAdapter`` in ``app/lab/sandbox/base.py``). It drives the real
``RefAgent`` loop against the project's Anthropic-compatible LLM endpoint and
streams protocol steps back to the Gateway. It holds NO DB/Redis/world handle —
its only outbound credential is the model endpoint — and it only INTENDS tool
calls; the Gateway's Broker mediates every effect.

Run standalone only with an explicit ``LAB_RUNTIME_PROTOCOL_VERSION`` and the
corresponding isolated Runtime configuration. The process fails closed when
that configuration is absent or incomplete.
The Gateway's ``SimverseRefAdapter`` points at this via
``settings.lab_simverse_ref_base_url``.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Mapping

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from app.config import settings
from app.lab import guard
from app.lab.protocol import (
    MAX_COMMAND_BYTES,
    MAX_EVENT_BYTES,
    MAX_UNACKED_BYTES,
    MAX_UNACKED_EVENTS,
    ControlCommand,
    RuntimeEvent,
    RuntimeV2Handshake,
    ToolResultCommand,
    args_digest,
    runtime_v2_supervision_handshake,
)
from app.lab.runtime_ref.agent import AgentTurn, RefAgent, anthropic_completer
from app.lab.runtime_ref.service_auth import (
    MAX_REQUEST_BYTES,
    RequestSchemaError,
    ServiceAuthConfig,
    ServiceAuthError,
    ServiceBinding,
    ServiceClaims,
    ServiceTokenValidator,
    StrictRequestModel,
    canonical_json_bytes,
    canonical_request_digest,
    extract_bearer_token,
)
from app.lab.runtime_ref.store import (
    CommandBinding,
    CrossBindingReplay,
    RuntimeStore,
    RuntimeStoreBackpressure,
    RuntimeStoreConflict,
    RuntimeStoreNotFound,
    StoredSession,
)


_RUNTIME_LOOP_VERSION = 1
_MAX_RUNTIME_JSON_DEPTH = 32


@dataclass
class _Session:
    session_id: str
    scopes: list[str]
    steps: list[dict] = field(default_factory=list)   # protocol step dicts with seq
    artifacts: list[dict] = field(default_factory=list)
    done: bool = False
    cancelled: bool = False
    task: asyncio.Task | None = None


_SESSIONS: dict[str, _Session] = {}


class StartBody(BaseModel):
    run_id: str
    scopes: list[str] = []
    budget_usd: float = 0.5
    egress_allowlist: list[str] = []


class GoalBody(BaseModel):
    brief: str
    scopes: list[str] = []


class ApproveBody(BaseModel):
    approval_id: str
    decision: bool


class V2StartBody(StrictRequestModel):
    schema_version: Literal[2]
    command_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    client_run_id: str = Field(min_length=1, max_length=200)
    epoch: int = Field(ge=0)
    scopes: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list
    )
    budget_usd: float = Field(ge=0, allow_inf_nan=False)
    egress_allowlist: list[
        Annotated[str, Field(min_length=1, max_length=500)]
    ] = Field(default_factory=list)


class V2GoalBody(StrictRequestModel):
    schema_version: Literal[2]
    command_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    epoch: int = Field(ge=0)
    brief: str = Field(min_length=1)
    scopes: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list
    )


class V2ApproveBody(StrictRequestModel):
    schema_version: Literal[2]
    command_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    epoch: int = Field(ge=0)
    approval_id: str = Field(min_length=1, max_length=200)
    decision: bool


class V2EventAckBody(StrictRequestModel):
    schema_version: Literal[2]
    command_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=200)
    epoch: int = Field(ge=0)
    cursor: int = Field(ge=0)


def _completer():
    from app.llm.client import get_client
    return anthropic_completer(get_client("system"), settings.llm_model)


def _create_v1_app(completer_factory, max_steps: int) -> FastAPI:
    app = FastAPI(title="Simverse Lab reference runtime", version="1.0")

    def _sess(sid: str) -> _Session:
        s = _SESSIONS.get(sid)
        if s is None:
            raise HTTPException(status_code=404, detail="session not found")
        return s

    @app.post("/runs")
    async def start_run(body: StartBody):
        sid = f"ref-{uuid.uuid4().hex[:12]}"
        _SESSIONS[sid] = _Session(session_id=sid, scopes=list(body.scopes))
        return {"session_id": sid}

    @app.post("/runs/{sid}/goal")
    async def submit_goal(sid: str, body: GoalBody):
        s = _sess(sid)
        scopes = body.scopes or s.scopes

        def on_step(step) -> None:
            seq = len(s.steps) + 1
            s.steps.append({
                "seq": seq, "phase": step.phase, "tool": step.tool,
                "summary": step.summary, "payload": step.payload,
                "model_tokens": step.model_tokens, "approval": step.approval,
            })

        # Run the loop to completion here (buffering steps via on_step), so the
        # Gateway's subsequent /steps poll gets every step + done in one shot. This
        # is robust across transports (a fire-and-forget background task is not
        # reliably driven by every ASGI server / test transport). Incremental live
        # streaming during a long run is a noted follow-up; the poll-with-cursor
        # protocol contract holds either way.
        agent = RefAgent(complete=completer_factory(), max_steps=max_steps)
        result = await agent.run(brief=body.brief, scopes=scopes,
                                 on_step=on_step, should_cancel=lambda: s.cancelled)
        s.artifacts = [
            {"kind": a.kind, "title": a.title, "uri": a.uri, "text_md": a.text_md, "meta": a.meta}
            for a in result.artifacts]
        s.done = True
        return {"ok": True}

    @app.get("/runs/{sid}/steps")
    async def get_steps(sid: str, after: int = 0):
        s = _sess(sid)
        fresh = [st for st in s.steps if st["seq"] > after]
        return {"steps": fresh, "done": s.done or s.cancelled}

    @app.post("/runs/{sid}/approve")
    async def approve(sid: str, body: ApproveBody):
        _sess(sid)
        return {"ok": True}

    @app.get("/runs/{sid}/artifacts")
    async def artifacts(sid: str):
        s = _sess(sid)
        return {"artifacts": s.artifacts}

    async def _teardown(s: _Session) -> None:
        s.cancelled = True
        if s.task is not None and not s.task.done():
            s.task.cancel()

    @app.post("/runs/{sid}/stop")
    async def stop(sid: str):
        await _teardown(_sess(sid))
        return {"ok": True}

    @app.post("/runs/{sid}/cancel")
    async def cancel(sid: str):
        await _teardown(_sess(sid))
        return {"ok": True}

    @app.post("/runs/{sid}/terminate")
    async def terminate(sid: str):
        await _teardown(_sess(sid))
        return {"ok": True}

    @app.post("/runs/{sid}/kill")
    async def kill(sid: str):
        await _teardown(_sess(sid))
        return {"ok": True}

    @app.get("/runs/{sid}/health")
    async def health(sid: str):
        s = _sess(sid)
        alive = not (s.done or s.cancelled)
        return {"alive": alive, "cancelled": s.cancelled}

    return app


def _create_v2_app(
    *,
    completer_factory,
    max_steps: int,
    runtime_store_path: str,
    service_auth: ServiceAuthConfig | Mapping[str, object],
) -> FastAPI:
    store = RuntimeStore(runtime_store_path)
    validator = ServiceTokenValidator(service_auth)
    app = FastAPI(title="Simverse Lab reference runtime", version="2.0")
    app.state.runtime_store = store
    app.state.service_token_validator = validator
    session_locks: dict[str, asyncio.Lock] = {}

    def _session_lock(session_id: str) -> asyncio.Lock:
        return session_locks.setdefault(session_id, asyncio.Lock())

    def _reject_duplicate_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def _reject_nonfinite_json(value: str):
        raise ValueError(f"non-finite JSON number: {value}")

    def _enforce_json_depth(value: Any) -> None:
        stack = [(value, 1)]
        while stack:
            item, depth = stack.pop()
            if depth > _MAX_RUNTIME_JSON_DEPTH:
                raise ValueError("request JSON exceeds depth cap")
            if isinstance(item, dict):
                stack.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                stack.extend((child, depth + 1) for child in item)

    async def _strict_json_body(request: Request, model_type):
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400, detail="invalid_content_length"
                ) from exc
            if declared_length < 0:
                raise HTTPException(status_code=400, detail="invalid_content_length")
            if declared_length > MAX_REQUEST_BYTES:
                raise HTTPException(status_code=413, detail="request_body_too_large")

        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > MAX_REQUEST_BYTES:
                raise HTTPException(status_code=413, detail="request_body_too_large")
        try:
            value = json.loads(
                bytes(raw),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite_json,
            )
            _enforce_json_depth(value)
            body = model_type.model_validate(
                value, strict=model_type is not ControlCommand
            )
            canonical_json_bytes(body, max_bytes=MAX_REQUEST_BYTES)
            return body
        except RequestSchemaError as exc:
            raise HTTPException(status_code=413, detail="request_body_too_large") from exc
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
            RecursionError,
        ) as exc:
            raise HTTPException(status_code=422, detail="invalid_request_body") from exc

    def _authenticate(
        authorization: str | None,
        *,
        action: str,
        expected_binding: ServiceBinding | None = None,
    ) -> ServiceClaims:
        try:
            token = extract_bearer_token(authorization)
            return validator.validate(
                token,
                required_action=action,
                expected_binding=expected_binding,
            )
        except ServiceAuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.reason) from exc

    def _authenticate_path(
        authorization: str | None, *, action: str, sid: str
    ) -> ServiceClaims:
        claims = _authenticate(authorization, action=action)
        if claims.session_id != sid:
            raise HTTPException(status_code=403, detail="binding_mismatch")
        return claims

    def _enforce_binding(
        claims: ServiceClaims, *, run_id: str, session_id: str, epoch: int
    ) -> None:
        if (
            claims.run_id != run_id
            or claims.session_id != session_id
            or claims.epoch != epoch
        ):
            raise HTTPException(status_code=403, detail="binding_mismatch")

    async def _session_for_claims(sid: str, claims: ServiceClaims) -> StoredSession:
        session = await store.get_session(sid)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        if session.run_id != claims.run_id or session.epoch != claims.epoch:
            raise HTTPException(status_code=403, detail="binding_mismatch")
        return session

    async def _bound_body_session(
        claims: ServiceClaims,
        *,
        sid: str,
        run_id: str,
        body_session_id: str,
        epoch: int,
    ) -> tuple[ServiceClaims, StoredSession]:
        _enforce_binding(
            claims, run_id=run_id, session_id=body_session_id, epoch=epoch
        )
        if sid != body_session_id:
            raise HTTPException(status_code=403, detail="binding_mismatch")
        return claims, await _session_for_claims(sid, claims)

    def _binding(
        body,
        claims: ServiceClaims,
        *,
        action: str,
    ) -> CommandBinding:
        return CommandBinding(
            audience=validator.config.audience,
            command_id=body.command_id,
            jti=claims.jti,
            request_digest=canonical_request_digest(body),
            run_id=body.run_id,
            session_id=getattr(body, "session_id", getattr(body, "client_run_id", "")),
            epoch=body.epoch,
            action=action,
        )

    async def _inspect_command(binding: CommandBinding):
        try:
            return await store.inspect_command(binding)
        except CrossBindingReplay as exc:
            raise HTTPException(
                status_code=403, detail="command_binding_mismatch"
            ) from exc

    async def _claim_command(binding: CommandBinding):
        try:
            return await store.claim_command(binding)
        except CrossBindingReplay as exc:
            raise HTTPException(
                status_code=403, detail="command_binding_mismatch"
            ) from exc

    async def _complete_result_receipt(
        *,
        binding: CommandBinding,
        claim,
        body: ToolResultCommand,
        session: StoredSession,
        loop: dict[str, Any],
    ) -> dict[str, Any]:
        candidate = {
            "receipt_id": claim.receipt.receipt_id,
            "request_digest": binding.request_digest,
            "session_id": session.session_id,
            "turn_id": body.turn_id,
            "intent_id": body.intent_id,
            "action_id": body.action_id,
            "state": "runtime_acked",
            "runtime_state": loop["phase"],
            "cursor": session.next_event_cursor - 1,
        }
        stored = loop.get("last_result_response")
        static_keys = (
            "receipt_id",
            "request_digest",
            "session_id",
            "turn_id",
            "intent_id",
            "action_id",
            "state",
        )
        if not (
            isinstance(stored, dict)
            and all(stored.get(key) == candidate[key] for key in static_keys)
            and stored.get("runtime_state") == loop["phase"]
            and type(stored.get("cursor")) is int
            and stored["cursor"] >= 0
        ):
            stored = candidate
            loop["last_result_response"] = stored
            await store.transition_session(
                session.session_id,
                expected_states=session.state,
                new_state=session.state,
                checkpoint=loop,
            )
        completed = await store.complete_command(binding, response=stored)
        return completed.response

    @staticmethod
    def _stable_uuid(*parts: object) -> str:
        value = ":".join(str(part) for part in parts)
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"simverse:runtime-v2:{value}"))

    @staticmethod
    def _turn_id(session_id: str, goal_command_id: str, sequence: int) -> str:
        binding = f"{session_id}:{goal_command_id}:{sequence}"
        return f"turn-{uuid.uuid5(uuid.NAMESPACE_URL, binding).hex}"

    @staticmethod
    def _loop_checkpoint(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("loop_version") != _RUNTIME_LOOP_VERSION:
            raise RuntimeStoreConflict("runtime loop checkpoint is missing or invalid")
        if value.get("phase") not in {
            "awaiting_model",
            "awaiting_result_model",
            "persisting_turn",
            "intent_pending",
            "completed",
            "failed",
        }:
            raise RuntimeStoreConflict("runtime loop checkpoint phase is invalid")
        if type(value.get("turn_sequence")) is not int or value["turn_sequence"] < 0:
            raise RuntimeStoreConflict("runtime loop turn sequence is invalid")
        if not isinstance(value.get("agent_checkpoint"), dict):
            raise RuntimeStoreConflict("runtime loop agent checkpoint is invalid")
        return value

    @staticmethod
    def _serialized_turn(turn: AgentTurn, *, turn_id: str) -> dict[str, Any]:
        artifact = None
        if turn.artifact is not None:
            artifact = {
                "kind": turn.artifact.kind,
                "title": guard.redact_text(turn.artifact.title) or "",
                "uri": turn.artifact.uri,
                "text_md": guard.redact_text(turn.artifact.text_md),
                "meta": guard.redact_payload(turn.artifact.meta),
            }
        tool_intent = None
        if turn.tool_intent is not None:
            tool_intent = [turn.tool_intent[0], turn.tool_intent[1]]
        return {
            "state": turn.state,
            "turn_id": turn_id,
            "agent_checkpoint": turn.checkpoint,
            "steps": [
                {
                    "phase": step.phase,
                    "tool": step.tool,
                    "summary": guard.redact_text(step.summary) or "",
                    "payload": step.payload,
                    "model_tokens": step.model_tokens,
                }
                for step in turn.steps
            ],
            "tool_intent": tool_intent,
            "artifact": artifact,
        }

    async def _append_event(
        session: StoredSession,
        *,
        event_kind: str,
        payload: dict,
        dedupe_key: str,
        turn_id: str | None = None,
        intent_id: str | None = None,
        outcome: str | None = None,
        tool_name: str | None = None,
        tool_args: dict | None = None,
    ):
        current = await store.get_session(session.session_id)
        if current is None:
            raise RuntimeStoreNotFound("session not found")
        if current.run_id != session.run_id or current.epoch != session.epoch:
            raise RuntimeStoreConflict("session event binding changed")
        event_id = _stable_uuid(session.session_id, dedupe_key)
        tool_digest = args_digest(tool_args) if tool_args is not None else None
        candidate = RuntimeEvent(
            event_id=event_id,
            run_id=session.run_id,
            session_id=session.session_id,
            cursor=current.next_event_cursor,
            epoch=session.epoch,
            event_kind=event_kind,
            turn_id=turn_id,
            intent_id=intent_id,
            outcome=outcome,
            tool_name=tool_name,
            tool_args=tool_args,
            tool_args_digest=tool_digest,
            payload=payload,
            occurred_at=datetime.now(UTC),
        )
        encoded_size = len(canonical_json_bytes(
            candidate.model_dump(mode="json"), max_bytes=MAX_EVENT_BYTES
        ))
        return await store.append_event(
            session.session_id,
            event_kind=candidate.event_kind,
            turn_id=candidate.turn_id,
            intent_id=candidate.intent_id,
            outcome=candidate.outcome,
            tool_name=candidate.tool_name,
            tool_args=candidate.tool_args,
            tool_args_digest=candidate.tool_args_digest,
            payload=candidate.payload,
            encoded_size=encoded_size,
            event_id=candidate.event_id,
            dedupe_key=dedupe_key,
        )

    async def _persist_turn(
        session: StoredSession,
        loop: dict[str, Any],
        *,
        expected_state: str,
    ) -> tuple[StoredSession, dict[str, Any]]:
        pending = loop.get("pending_turn")
        if not isinstance(pending, dict):
            raise RuntimeStoreConflict("runtime turn output was not checkpointed")
        turn_id = pending.get("turn_id")
        if not isinstance(turn_id, str) or not turn_id:
            raise RuntimeStoreConflict("runtime turn id is invalid")
        steps = pending.get("steps")
        if not isinstance(steps, list) or not steps:
            raise RuntimeStoreConflict("runtime turn has no model steps")
        think = steps[0]
        await _append_event(
            session,
            event_kind="think",
            turn_id=turn_id,
            payload={
                "summary": think.get("summary", ""),
                "model_tokens": think.get("model_tokens", 0),
            },
            dedupe_key=f"turn:{turn_id}:think",
        )

        state = pending.get("state")
        loop["agent_checkpoint"] = pending["agent_checkpoint"]
        loop["last_turn_id"] = turn_id
        loop["turn_sequence"] += 1
        provenance = loop.get("broker_results") or []
        latest_result = provenance[-1] if provenance else None
        broker_terminal_failure = (
            isinstance(latest_result, dict)
            and latest_result.get("outcome") in {"denied", "failed"}
        )
        if state == "intent":
            if broker_terminal_failure:
                reason = steps[-1].get(
                    "summary", f"broker result {latest_result['outcome']}"
                )
                await _append_event(
                    session,
                    event_kind="failed",
                    turn_id=turn_id,
                    payload={"summary": reason},
                    dedupe_key=f"turn:{turn_id}:failed",
                )
                loop.update({
                    "phase": "failed",
                    "active_intent_id": None,
                    "pending_turn": None,
                })
                updated = await store.transition_session(
                    session.session_id,
                    expected_states=expected_state,
                    new_state="failed",
                    checkpoint=loop,
                )
                return updated, loop
            raw_intent = pending.get("tool_intent")
            if (
                not isinstance(raw_intent, list)
                or len(raw_intent) != 2
                or not isinstance(raw_intent[0], str)
                or not isinstance(raw_intent[1], dict)
            ):
                raise RuntimeStoreConflict("runtime tool intent is invalid")
            tool, args = raw_intent
            intent_id = f"intent-{_stable_uuid(session.session_id, turn_id, tool)}"
            await store.record_intent(
                session.session_id,
                turn_id=turn_id,
                intent_id=intent_id,
                tool=tool,
                args=args,
            )
            summary = steps[-1].get("summary", "")
            await _append_event(
                session,
                event_kind="tool_intent",
                turn_id=turn_id,
                intent_id=intent_id,
                tool_name=tool,
                tool_args=args,
                payload={"summary": summary},
                dedupe_key=f"intent:{intent_id}",
            )
            loop.update({
                "phase": "intent_pending",
                "active_intent_id": intent_id,
                "pending_turn": None,
            })
            updated = await store.transition_session(
                session.session_id,
                expected_states=expected_state,
                new_state="intent_pending",
                checkpoint=loop,
            )
            return updated, loop

        if state == "final":
            summary = steps[-1].get("summary", "")
            await _append_event(
                session,
                event_kind="final",
                turn_id=turn_id,
                payload={"summary": summary},
                dedupe_key=f"turn:{turn_id}:final",
            )
            if broker_terminal_failure:
                loop.update({
                    "phase": "failed",
                    "active_intent_id": None,
                    "pending_turn": None,
                })
                updated = await store.transition_session(
                    session.session_id,
                    expected_states=expected_state,
                    new_state="failed",
                    checkpoint=loop,
                )
                return updated, loop
            artifact = pending.get("artifact")
            if not isinstance(artifact, dict):
                raise RuntimeStoreConflict("final turn has no artifact")
            meta = dict(artifact.get("meta") or {})
            if provenance:
                latest = provenance[-1]
                meta.update({
                    "broker_result_digest": latest["result_digest"],
                    "broker_result_provenance": {
                        "command_id": latest["command_id"],
                        "intent_id": latest["intent_id"],
                        "action_id": latest["action_id"],
                    },
                    "broker_results": provenance,
                })
            await store.put_artifact(
                session.session_id,
                artifact_id=_stable_uuid(session.session_id, "artifact", turn_id),
                kind=artifact["kind"],
                title=artifact["title"],
                uri=artifact.get("uri"),
                text_md=artifact.get("text_md"),
                meta=meta,
            )
            loop.update({
                "phase": "completed",
                "active_intent_id": None,
                "pending_turn": None,
            })
            updated = await store.transition_session(
                session.session_id,
                expected_states=expected_state,
                new_state="completed",
                checkpoint=loop,
            )
            return updated, loop

        if state != "failed":
            raise RuntimeStoreConflict("runtime turn terminal state is invalid")
        reason = steps[-1].get("summary", "runtime model turn failed")
        await _append_event(
            session,
            event_kind="failed",
            turn_id=turn_id,
            payload={"summary": reason},
            dedupe_key=f"turn:{turn_id}:failed",
        )
        loop.update({
            "phase": "failed",
            "active_intent_id": None,
            "pending_turn": None,
        })
        updated = await store.transition_session(
            session.session_id,
            expected_states=expected_state,
            new_state="failed",
            checkpoint=loop,
        )
        return updated, loop

    @app.get("/handshake")
    async def handshake_v2(
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        _authenticate(authorization, action="runtime.handshake")
        manifest = RuntimeV2Handshake(
            protocol_version=2,
            provider_name="simverse_ref",
            durability_class="session_affine",
            reattach_capability="client_run_id",
            effect_mode="broker_only",
            capabilities=sorted({
                "backpressure",
                "broker_mediation",
                "cancel",
                "control",
                "cursor_replay",
                "events_ack",
                "idempotent_create",
                "kill",
                "reattach",
                "result_receipts",
                "scoped_auth",
                "terminate",
            }),
        )
        return runtime_v2_supervision_handshake(manifest).model_dump(mode="json")

    @app.get("/livez")
    async def livez():
        return {"alive": True, "protocol_version": 2}

    @app.post("/runs", status_code=201)
    async def start_run_v2(
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        claims = _authenticate(authorization, action="session.create")
        body = await _strict_json_body(request, V2StartBody)
        _enforce_binding(
            claims,
            run_id=body.run_id,
            session_id=body.client_run_id,
            epoch=body.epoch,
        )
        request_digest = canonical_request_digest(body)
        binding = CommandBinding(
            audience=validator.config.audience,
            command_id=body.command_id,
            jti=claims.jti,
            request_digest=request_digest,
            run_id=body.run_id,
            session_id=body.client_run_id,
            epoch=body.epoch,
            action="session.create",
        )
        try:
            claim = await store.claim_command(binding)
            if claim.is_retry and claim.receipt.state == "completed":
                return claim.receipt.response
            session = await store.create_or_get_session(
                run_id=body.run_id,
                client_run_id=body.client_run_id,
                epoch=body.epoch,
                scopes=body.scopes,
                budget_usd=body.budget_usd,
                egress_allowlist=body.egress_allowlist,
            )
            started = await _append_event(
                session,
                event_kind="session_started",
                payload={},
                dedupe_key="session:started",
            )
            response = {
                "session_id": session.session_id,
                "receipt_id": claim.receipt.receipt_id,
                "request_digest": request_digest,
                "cursor": started.cursor,
            }
            completed = await store.complete_command(binding, response=response)
            return completed.response
        except CrossBindingReplay as exc:
            raise HTTPException(status_code=403, detail="command_binding_mismatch") from exc
        except RuntimeStoreConflict as exc:
            raise HTTPException(status_code=409, detail="session_binding_conflict") from exc

    @app.post("/runs/{sid}/goal")
    async def submit_goal_v2(
        sid: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        claims = _authenticate_path(
            authorization, action="goal.submit", sid=sid
        )
        body = await _strict_json_body(request, V2GoalBody)
        _enforce_binding(
            claims,
            run_id=body.run_id,
            session_id=body.session_id,
            epoch=body.epoch,
        )
        if sid != body.session_id:
            raise HTTPException(status_code=403, detail="binding_mismatch")
        binding = _binding(body, claims, action="goal.submit")
        known = await _inspect_command(binding)
        if known is not None and known.state == "completed":
            return known.response
        session = await _session_for_claims(sid, claims)
        requested_scopes = tuple(sorted(set(body.scopes or session.scopes)))
        if not set(requested_scopes) <= set(session.scopes):
            raise HTTPException(status_code=403, detail="scope_escalation")
        claim = await _claim_command(binding)
        if claim.is_retry and claim.receipt.state == "completed":
            return claim.receipt.response

        async with _session_lock(sid):
            known = await _inspect_command(binding)
            if known is not None and known.state == "completed":
                return known.response
            session = await _session_for_claims(sid, claims)
            try:
                if session.state == "created":
                    loop = {
                        "loop_version": _RUNTIME_LOOP_VERSION,
                        "goal_command_id": body.command_id,
                        "goal_request_digest": binding.request_digest,
                        "agent_checkpoint": RefAgent.initial_checkpoint(
                            brief=body.brief,
                            scopes=list(requested_scopes),
                        ),
                        "phase": "awaiting_model",
                        "turn_sequence": 0,
                        "active_intent_id": None,
                        "pending_turn": None,
                        "broker_results": [],
                    }
                    try:
                        canonical_json_bytes(loop, max_bytes=MAX_UNACKED_BYTES)
                    except RequestSchemaError as exc:
                        raise HTTPException(
                            status_code=413,
                            detail="runtime_checkpoint_too_large",
                        ) from exc
                    session = await store.transition_session(
                        sid,
                        expected_states="created",
                        new_state="running",
                        checkpoint=loop,
                    )
                else:
                    loop = _loop_checkpoint(session.checkpoint)
                    if (
                        loop.get("goal_command_id") != body.command_id
                        or loop.get("goal_request_digest") != binding.request_digest
                    ):
                        raise RuntimeStoreConflict(
                            "session is already bound to another goal"
                        )

                if loop["phase"] == "awaiting_model":
                    agent = RefAgent(
                        complete=completer_factory(), max_steps=max_steps
                    )
                    turn = await agent.advance_turn(loop["agent_checkpoint"])
                    turn_id = _turn_id(
                        sid, body.command_id, loop["turn_sequence"]
                    )
                    loop["pending_turn"] = _serialized_turn(
                        turn, turn_id=turn_id
                    )
                    loop["phase"] = "persisting_turn"
                    session = await store.transition_session(
                        sid,
                        expected_states="running",
                        new_state="running",
                        checkpoint=loop,
                    )

                if loop["phase"] == "persisting_turn":
                    session, loop = await _persist_turn(
                        session, loop, expected_state="running"
                    )
                if loop["phase"] not in {"intent_pending", "completed", "failed"}:
                    raise RuntimeStoreConflict("goal did not reach a durable pause")

                response = {
                    "receipt_id": claim.receipt.receipt_id,
                    "request_digest": binding.request_digest,
                    "session_id": sid,
                    "turn_id": loop["last_turn_id"],
                    "state": loop["phase"],
                    "cursor": session.next_event_cursor - 1,
                }
                completed = await store.complete_command(
                    binding, response=response
                )
                return completed.response
            except RuntimeStoreBackpressure as exc:
                raise HTTPException(status_code=429, detail="event_backpressure") from exc
            except RequestSchemaError as exc:
                raise HTTPException(
                    status_code=413, detail="runtime_checkpoint_too_large"
                ) from exc
            except (RuntimeStoreConflict, RuntimeStoreNotFound, ValueError) as exc:
                raise HTTPException(status_code=409, detail="runtime_goal_conflict") from exc

    @app.post("/runs/{sid}/results")
    async def submit_result_v2(
        sid: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        claims = _authenticate_path(
            authorization, action="tool_result.submit", sid=sid
        )
        body = await _strict_json_body(request, ToolResultCommand)
        _enforce_binding(
            claims,
            run_id=body.run_id,
            session_id=body.session_id,
            epoch=body.epoch,
        )
        if sid != body.session_id:
            raise HTTPException(status_code=403, detail="binding_mismatch")
        redacted_payload = guard.redact_payload(body.payload)
        try:
            canonical_json_bytes(
                body.payload, max_bytes=MAX_COMMAND_BYTES
            )
            canonical_json_bytes(
                redacted_payload, max_bytes=MAX_COMMAND_BYTES
            )
        except RequestSchemaError as exc:
            raise HTTPException(
                status_code=413, detail="model_result_payload_too_large"
            ) from exc

        binding = _binding(body, claims, action="tool_result.submit")
        known = await _inspect_command(binding)
        if known is not None and known.state == "completed":
            return known.response
        session = await _session_for_claims(sid, claims)
        claim = await _claim_command(binding)
        if claim.is_retry and claim.receipt.state == "completed":
            return claim.receipt.response

        async with _session_lock(sid):
            known = await _inspect_command(binding)
            if known is not None and known.state == "completed":
                return known.response
            session = await _session_for_claims(sid, claims)
            try:
                loop = _loop_checkpoint(session.checkpoint)
                intent = await store.get_intent(sid, body.intent_id)
                if intent is None:
                    raise RuntimeStoreNotFound("result intent does not exist")
                if intent.turn_id != body.turn_id:
                    raise RuntimeStoreConflict("result turn binding mismatch")

                if (
                    loop.get("last_result_command_id") == body.command_id
                    and intent.state == "applied"
                    and loop["phase"] in {"intent_pending", "completed", "failed"}
                    and session.state == loop["phase"]
                ):
                    await store.resolve_intent(
                        sid,
                        intent_id=body.intent_id,
                        turn_id=body.turn_id,
                        command_id=body.command_id,
                        action_id=body.action_id,
                        result_digest=body.result_digest,
                        outcome=body.outcome,
                        payload=body.payload,
                        stored_payload=redacted_payload,
                    )
                    return await _complete_result_receipt(
                        binding=binding,
                        claim=claim,
                        body=body,
                        session=session,
                        loop=loop,
                    )

                if session.state == "intent_pending":
                    if (
                        loop["phase"] != "intent_pending"
                        or loop.get("active_intent_id") != body.intent_id
                    ):
                        raise RuntimeStoreConflict("result is not for the active intent")
                    intent = await store.resolve_intent(
                        sid,
                        intent_id=body.intent_id,
                        turn_id=body.turn_id,
                        command_id=body.command_id,
                        action_id=body.action_id,
                        result_digest=body.result_digest,
                        outcome=body.outcome,
                        payload=body.payload,
                        stored_payload=redacted_payload,
                    )
                    result_event = await _append_event(
                        session,
                        event_kind="tool_result",
                        turn_id=body.turn_id,
                        intent_id=body.intent_id,
                        outcome=body.outcome,
                        payload=redacted_payload,
                        dedupe_key=f"result:{body.intent_id}",
                    )
                    provenance = {
                        "command_id": body.command_id,
                        "intent_id": body.intent_id,
                        "action_id": body.action_id,
                        "outcome": body.outcome,
                        "result_digest": body.result_digest,
                    }
                    existing_results = loop.setdefault("broker_results", [])
                    if provenance not in existing_results:
                        existing_results.append(provenance)
                    loop.update({
                        "phase": "awaiting_result_model",
                        "resume_result": {
                            "command_id": body.command_id,
                            "intent_id": body.intent_id,
                            "turn_id": body.turn_id,
                            "tool": intent.tool,
                            "outcome": intent.result_outcome,
                            "payload": intent.result_payload,
                            "result_event_cursor": result_event.cursor,
                        },
                        "last_result_command_id": body.command_id,
                    })
                    session = await store.transition_session(
                        sid,
                        expected_states="intent_pending",
                        new_state="resuming",
                        checkpoint=loop,
                    )
                elif session.state == "resuming":
                    if (
                        loop.get("last_result_command_id") != body.command_id
                        or loop.get("resume_result", {}).get("intent_id")
                        != body.intent_id
                    ):
                        raise RuntimeStoreConflict(
                            "session is resuming another result command"
                        )
                    await store.resolve_intent(
                        sid,
                        intent_id=body.intent_id,
                        turn_id=body.turn_id,
                        command_id=body.command_id,
                        action_id=body.action_id,
                        result_digest=body.result_digest,
                        outcome=body.outcome,
                        payload=body.payload,
                        stored_payload=redacted_payload,
                    )
                elif loop.get("last_result_command_id") != body.command_id:
                    raise RuntimeStoreConflict("session no longer accepts this result")

                if loop["phase"] == "awaiting_result_model":
                    resume = loop["resume_result"]
                    agent = RefAgent(
                        complete=completer_factory(), max_steps=max_steps
                    )
                    turn = await agent.resume_turn(
                        checkpoint=loop["agent_checkpoint"],
                        tool=resume["tool"],
                        outcome=resume["outcome"],
                        payload=resume["payload"],
                    )
                    turn_id = _turn_id(
                        sid,
                        loop["goal_command_id"],
                        loop["turn_sequence"],
                    )
                    loop["pending_turn"] = _serialized_turn(
                        turn, turn_id=turn_id
                    )
                    loop["phase"] = "persisting_turn"
                    session = await store.transition_session(
                        sid,
                        expected_states="resuming",
                        new_state="resuming",
                        checkpoint=loop,
                    )

                if loop["phase"] == "persisting_turn":
                    await store.mark_intent_applied(sid, body.intent_id)
                    session, loop = await _persist_turn(
                        session, loop, expected_state="resuming"
                    )
                if loop["phase"] not in {"intent_pending", "completed", "failed"}:
                    raise RuntimeStoreConflict("result did not reach a durable pause")

                return await _complete_result_receipt(
                    binding=binding,
                    claim=claim,
                    body=body,
                    session=session,
                    loop=loop,
                )
            except RuntimeStoreBackpressure as exc:
                raise HTTPException(status_code=429, detail="event_backpressure") from exc
            except RequestSchemaError as exc:
                raise HTTPException(
                    status_code=413, detail="runtime_checkpoint_too_large"
                ) from exc
            except (RuntimeStoreConflict, RuntimeStoreNotFound, ValueError) as exc:
                raise HTTPException(status_code=409, detail="runtime_result_conflict") from exc

    @app.get("/runs/{sid}/events")
    async def events_v2(
        sid: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        claims = _authenticate_path(
            authorization, action="events.read", sid=sid
        )
        def _bounded_query_int(
            name: str, *, default: int, minimum: int, maximum: int
        ) -> int:
            values = request.query_params.getlist(name)
            if not values:
                return default
            if len(values) != 1 or not values[0].isdecimal():
                raise HTTPException(
                    status_code=422, detail=f"invalid event {name}"
                )
            value = int(values[0])
            if not minimum <= value <= maximum:
                raise HTTPException(
                    status_code=422, detail=f"invalid event {name}"
                )
            return value

        after = _bounded_query_int(
            "after", default=0, minimum=0, maximum=2**63 - 1
        )
        limit = _bounded_query_int(
            "limit",
            default=MAX_UNACKED_EVENTS,
            minimum=1,
            maximum=MAX_UNACKED_EVENTS,
        )
        max_bytes = _bounded_query_int(
            "max_bytes",
            default=MAX_UNACKED_BYTES,
            minimum=1,
            maximum=MAX_UNACKED_BYTES,
        )
        session = await _session_for_claims(sid, claims)
        latest_cursor = session.next_event_cursor - 1
        if after > latest_cursor:
            raise HTTPException(status_code=422, detail="invalid event cursor")
        try:
            events = await store.list_events(sid, after=after, limit=limit)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid event cursor") from exc
        encoded_events: list[dict[str, Any]] = []
        encoded_bytes = 0
        for event in events:
            envelope = RuntimeEvent(
                event_id=event.event_id,
                run_id=session.run_id,
                session_id=session.session_id,
                cursor=event.cursor,
                epoch=session.epoch,
                event_kind=event.event_kind,
                turn_id=event.turn_id,
                intent_id=event.intent_id,
                outcome=event.outcome,
                tool_name=event.tool_name,
                tool_args=event.tool_args,
                tool_args_digest=event.tool_args_digest,
                payload=event.payload,
                occurred_at=event.occurred_at,
            ).model_dump(mode="json")
            size = len(canonical_json_bytes(envelope, max_bytes=MAX_EVENT_BYTES))
            if encoded_bytes + size > max_bytes:
                break
            encoded_events.append(envelope)
            encoded_bytes += size
        delivered_through = (
            encoded_events[-1]["cursor"] if encoded_events else after
        )
        has_more = delivered_through < latest_cursor
        return {
            "events": encoded_events,
            "done": (
                session.state in {"completed", "failed", "cancelled"}
                and not has_more
            ),
            "has_more": has_more,
            "latest_cursor": latest_cursor,
            "acked_through": session.acked_event_cursor,
        }

    @app.post("/runs/{sid}/events/ack")
    async def acknowledge_events_v2(
        sid: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        claims = _authenticate_path(
            authorization, action="events.ack", sid=sid
        )
        body = await _strict_json_body(request, V2EventAckBody)
        _enforce_binding(
            claims,
            run_id=body.run_id,
            session_id=body.session_id,
            epoch=body.epoch,
        )
        if sid != body.session_id:
            raise HTTPException(status_code=403, detail="binding_mismatch")
        binding = _binding(body, claims, action="events.ack")
        known = await _inspect_command(binding)
        if known is not None and known.state == "completed":
            return known.response
        session = await _session_for_claims(sid, claims)
        if (
            body.cursor > session.next_event_cursor - 1
            or body.cursor < session.acked_event_cursor
        ):
            raise HTTPException(status_code=409, detail="event_ack_conflict")
        claim = await _claim_command(binding)
        if claim.is_retry and claim.receipt.state == "completed":
            return claim.receipt.response
        async with _session_lock(sid):
            known = await _inspect_command(binding)
            if known is not None and known.state == "completed":
                return known.response
            try:
                session = await store.acknowledge_events(
                    sid, cursor=body.cursor
                )
                response = {
                    "receipt_id": claim.receipt.receipt_id,
                    "request_digest": binding.request_digest,
                    "session_id": sid,
                    "acked_through": session.acked_event_cursor,
                }
                completed = await store.complete_command(
                    binding, response=response
                )
                return completed.response
            except (RuntimeStoreConflict, RuntimeStoreNotFound, ValueError) as exc:
                raise HTTPException(status_code=409, detail="event_ack_conflict") from exc

    @app.get("/runs/{sid}/steps")
    async def steps_v2(
        sid: str,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        claims = _authenticate_path(
            authorization, action="events.read", sid=sid
        )
        await _session_for_claims(sid, claims)
        raise HTTPException(
            status_code=501, detail="protocol-v2 uses the RuntimeEvent stream"
        )

    @app.get("/runs/{sid}/artifacts")
    async def artifacts_v2(
        sid: str,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        claims = _authenticate_path(
            authorization, action="artifacts.read", sid=sid
        )
        session = await _session_for_claims(sid, claims)
        if session.state != "completed" or await store.count_active_intents(sid):
            raise HTTPException(status_code=409, detail="artifacts pending")
        artifacts = await store.list_artifacts(sid)
        return {
            "artifacts": [
                {
                    "artifact_id": artifact.artifact_id,
                    "kind": artifact.kind,
                    "title": artifact.title,
                    "uri": artifact.uri,
                    "text_md": artifact.text_md,
                    "meta": artifact.meta,
                }
                for artifact in artifacts
            ]
        }

    @app.post("/runs/{sid}/approve")
    async def approve_v2(
        sid: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        claims = _authenticate_path(
            authorization, action="runtime.control", sid=sid
        )
        body = await _strict_json_body(request, V2ApproveBody)
        await _bound_body_session(
            claims,
            sid=sid,
            run_id=body.run_id,
            body_session_id=body.session_id,
            epoch=body.epoch,
        )
        raise HTTPException(
            status_code=501, detail="protocol-v2 approval control is not implemented"
        )

    async def _control_runtime(
        sid: str,
        request: Request,
        authorization: str | None,
        *,
        action: Literal["cancel", "terminate", "kill"],
    ) -> dict:
        claims = _authenticate_path(
            authorization, action="runtime.control", sid=sid
        )
        body = await _strict_json_body(request, ControlCommand)
        _enforce_binding(
            claims,
            run_id=body.run_id,
            session_id=body.session_id,
            epoch=body.epoch,
        )
        if (
            sid != body.session_id
            or body.target_kind != "runtime"
            or body.target_id != sid
            or body.action != action
        ):
            raise HTTPException(status_code=403, detail="binding_mismatch")

        binding = _binding(body, claims, action="runtime.control")
        known = await _inspect_command(binding)
        if known is not None and known.state == "completed":
            return known.response
        session = await store.get_session(sid)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        if session.run_id != body.run_id or body.epoch < session.epoch:
            raise HTTPException(status_code=403, detail="binding_mismatch")
        claim = await _claim_command(binding)
        if claim.is_retry and claim.receipt.state == "completed":
            return claim.receipt.response

        async with _session_lock(sid):
            known = await _inspect_command(binding)
            if known is not None and known.state == "completed":
                return known.response
            session = await store.get_session(sid)
            if session is None:
                raise HTTPException(status_code=404, detail="session not found")
            if session.run_id != body.run_id or body.epoch < session.epoch:
                raise HTTPException(status_code=403, detail="binding_mismatch")
            if session.state not in {"completed", "failed", "cancelled"}:
                try:
                    session = await store.transition_session(
                        sid,
                        expected_states=session.state,
                        new_state="cancelled",
                    )
                except (RuntimeStoreConflict, RuntimeStoreNotFound) as exc:
                    raise HTTPException(
                        status_code=409, detail="runtime_control_conflict"
                    ) from exc
            response = {
                "receipt_id": claim.receipt.receipt_id,
                "request_digest": binding.request_digest,
                "request_id": body.request_id,
                "run_id": body.run_id,
                "session_id": sid,
                "target_id": body.target_id,
                "action": body.action,
                "epoch": body.epoch,
                "status": "confirmed_stopped",
                "runtime_state": session.state,
            }
            try:
                completed = await store.complete_command(
                    binding, response=response
                )
            except (RuntimeStoreConflict, RuntimeStoreNotFound) as exc:
                raise HTTPException(
                    status_code=409, detail="runtime_control_conflict"
                ) from exc
            return completed.response

    @app.post("/runs/{sid}/stop")
    async def stop_v2(
        sid: str,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        claims = _authenticate_path(
            authorization, action="runtime.control", sid=sid
        )
        await _session_for_claims(sid, claims)
        raise HTTPException(
            status_code=501,
            detail="protocol-v2 stop cleanup is not a durable control action",
        )

    @app.post("/runs/{sid}/cancel")
    async def cancel_v2(
        sid: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        return await _control_runtime(
            sid, request, authorization, action="cancel"
        )

    @app.post("/runs/{sid}/terminate")
    async def terminate_v2(
        sid: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        return await _control_runtime(
            sid, request, authorization, action="terminate"
        )

    @app.post("/runs/{sid}/kill")
    async def kill_v2(
        sid: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        return await _control_runtime(
            sid, request, authorization, action="kill"
        )

    @app.get("/runs/{sid}/health")
    async def health_v2(
        sid: str,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        claims = _authenticate_path(
            authorization, action="runtime.control", sid=sid
        )
        session = await _session_for_claims(sid, claims)
        return {
            "alive": session.state not in {"completed", "failed", "cancelled"},
            "cancelled": session.state == "cancelled",
        }

    return app


def create_app(
    completer_factory=_completer,
    max_steps: int = 3,
    *,
    protocol_version: int = 1,
    runtime_store_path: str | None = None,
    service_auth: ServiceAuthConfig | Mapping[str, object] | None = None,
) -> FastAPI:
    if protocol_version == 1:
        return _create_v1_app(completer_factory, max_steps)
    if protocol_version != 2:
        raise ValueError(f"unsupported Runtime protocol version {protocol_version}")
    if not runtime_store_path:
        raise ValueError("protocol-v2 requires runtime_store_path")
    if service_auth is None:
        raise ValueError("protocol-v2 requires service_auth")
    return _create_v2_app(
        completer_factory=completer_factory,
        max_steps=max_steps,
        runtime_store_path=runtime_store_path,
        service_auth=service_auth,
    )


def create_entrypoint_app(
    environ: Mapping[str, str] | None = None,
) -> FastAPI:
    """Build the standalone process app from its isolated trust-plane env."""
    env = os.environ if environ is None else environ
    raw_version = env.get("LAB_RUNTIME_PROTOCOL_VERSION")
    if raw_version not in {"1", "2"}:
        raise ValueError(
            "LAB_RUNTIME_PROTOCOL_VERSION must explicitly be 1 or 2"
        )
    if raw_version == "1":
        return create_app(protocol_version=1)

    store_path = env.get("LAB_RUNTIME_STORE_PATH", "")
    issuer = env.get("LAB_RUNTIME_AUTH_ISSUER", "")
    audience = env.get("LAB_RUNTIME_AUTH_AUDIENCE", "")
    raw_keys = env.get("LAB_RUNTIME_AUTH_KEYS_JSON", "")
    if not store_path or not issuer or not audience or not raw_keys:
        raise ValueError(
            "protocol-v2 standalone Runtime requires store path, issuer, "
            "audience, and key ring"
        )
    if audience != "lab-runtime":
        raise ValueError("protocol-v2 standalone Runtime audience must be lab-runtime")
    try:
        keys = json.loads(raw_keys)
    except json.JSONDecodeError as exc:
        raise ValueError("LAB_RUNTIME_AUTH_KEYS_JSON must be a JSON object") from exc
    if not isinstance(keys, dict) or len(keys) < 2:
        raise ValueError(
            "protocol-v2 standalone Runtime requires current and next auth keys"
        )
    return create_app(
        protocol_version=2,
        runtime_store_path=store_path,
        service_auth={
            "issuer": issuer,
            "audience": audience,
            "keys": keys,
        },
    )


def _disabled_entrypoint_app() -> FastAPI:
    disabled = FastAPI(
        title="Simverse Lab reference runtime (not configured)", version="0"
    )

    @disabled.get("/livez", status_code=503)
    async def disabled_livez():
        return {"alive": False, "reason": "runtime_protocol_not_configured"}

    return disabled


if __name__ == "__main__":
    try:
        app = create_entrypoint_app()
    except ValueError as exc:
        raise SystemExit(f"Runtime configuration error: {exc}") from exc
else:
    # Importing ``module:app`` must never silently select the legacy unauthenticated
    # protocol. The supported standalone entrypoint is ``python -m`` above.
    app = _disabled_entrypoint_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8900, log_level="warning")
