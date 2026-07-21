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
from typing import Annotated, Literal, Mapping

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError

from app.config import settings
from app.lab.protocol import ToolResultCommand
from app.lab.runtime_ref.agent import RefAgent, anthropic_completer
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
    RuntimeStoreConflict,
    StoredSession,
)


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
    runtime_store_path: str,
    service_auth: ServiceAuthConfig | Mapping[str, object],
) -> FastAPI:
    store = RuntimeStore(runtime_store_path)
    validator = ServiceTokenValidator(service_auth)
    app = FastAPI(title="Simverse Lab reference runtime", version="2.0")
    app.state.runtime_store = store
    app.state.service_token_validator = validator

    def _reject_duplicate_keys(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def _reject_nonfinite_json(value: str):
        raise ValueError(f"non-finite JSON number: {value}")

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
            body = model_type.model_validate(value, strict=True)
            canonical_json_bytes(body, max_bytes=MAX_REQUEST_BYTES)
            return body
        except RequestSchemaError as exc:
            raise HTTPException(status_code=413, detail="request_body_too_large") from exc
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
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
            response = {
                "session_id": session.session_id,
                "receipt_id": claim.receipt.receipt_id,
                "request_digest": request_digest,
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
        await _bound_body_session(
            claims,
            sid=sid,
            run_id=body.run_id,
            body_session_id=body.session_id,
            epoch=body.epoch,
        )
        raise HTTPException(
            status_code=501, detail="protocol-v2 goal loop is not implemented"
        )

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
        await _bound_body_session(
            claims,
            sid=sid,
            run_id=body.run_id,
            body_session_id=body.session_id,
            epoch=body.epoch,
        )
        raise HTTPException(
            status_code=501, detail="protocol-v2 result loop is not implemented"
        )

    @app.get("/runs/{sid}/events")
    async def events_v2(
        sid: str,
        request: Request,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        claims = _authenticate_path(
            authorization, action="events.read", sid=sid
        )
        after_values = request.query_params.getlist("after")
        if not after_values:
            after = 0
        elif len(after_values) == 1 and after_values[0].isdecimal():
            after = int(after_values[0])
        else:
            raise HTTPException(status_code=422, detail="invalid event cursor")
        session = await _session_for_claims(sid, claims)
        try:
            events = await store.list_events(sid, after=after)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid event cursor") from exc
        return {
            "events": [
                {
                    "cursor": event.cursor,
                    "event_id": event.event_id,
                    "event_kind": event.event_kind,
                    "turn_id": event.turn_id,
                    "intent_id": event.intent_id,
                    "outcome": event.outcome,
                    "payload": event.payload,
                }
                for event in events
            ],
            "done": session.state in {"completed", "failed", "cancelled"},
        }

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

    async def _control_scaffold(
        sid: str, authorization: str | None
    ) -> None:
        claims = _authenticate_path(
            authorization, action="runtime.control", sid=sid
        )
        await _session_for_claims(sid, claims)
        raise HTTPException(
            status_code=501, detail="protocol-v2 runtime control is not implemented"
        )

    @app.post("/runs/{sid}/stop")
    async def stop_v2(
        sid: str,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        await _control_scaffold(sid, authorization)

    @app.post("/runs/{sid}/cancel")
    async def cancel_v2(
        sid: str,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        await _control_scaffold(sid, authorization)

    @app.post("/runs/{sid}/terminate")
    async def terminate_v2(
        sid: str,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        await _control_scaffold(sid, authorization)

    @app.post("/runs/{sid}/kill")
    async def kill_v2(
        sid: str,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        await _control_scaffold(sid, authorization)

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
