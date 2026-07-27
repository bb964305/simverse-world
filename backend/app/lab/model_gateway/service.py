from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.lab.model_gateway.auth import (
    GatewayAuthError,
    GatewayClaims,
    renew_gateway_token,
    verify_gateway_token,
)
from app.lab.model_gateway.config import GatewayConfig
from app.lab.model_gateway.ledger import (
    BudgetReservationError,
    InflightLimitError,
    RunRevokedError,
    UsageLedger,
    UsageUnknownError,
)
from app.lab.model_gateway.translation import (
    TranslationError,
    chat_to_response,
    response_events,
    responses_to_chat,
)


class ReasoningStore:
    def __init__(self, *, ttl_s: int, max_entries: int) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._values: OrderedDict[tuple[str, str], tuple[float, str]] = OrderedDict()
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.ttl_s
        stale = [key for key, (stored_at, _) in self._values.items() if stored_at < cutoff]
        for key in stale:
            self._values.pop(key, None)
        while len(self._values) > self.max_entries:
            self._values.popitem(last=False)

    def get(self, run_id: str, call_id: str) -> str | None:
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            key = (run_id, call_id)
            value = self._values.get(key)
            if value is None:
                return None
            self._values.move_to_end(key)
            return value[1]

    def put_many(self, run_id: str, values: dict[str, str]) -> None:
        with self._lock:
            now = time.monotonic()
            for call_id, reasoning in values.items():
                key = (run_id, call_id)
                self._values[key] = (now, reasoning)
                self._values.move_to_end(key)
            self._prune(now)


def _estimated_input_tokens(raw: bytes) -> int:
    # One token per UTF-8 byte plus fixed protocol overhead is deliberately
    # pessimistic for both ASCII and CJK requests.
    return max(1, len(raw) + 512)


def _upstream_usage(payload: object) -> tuple[int, int, int] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):
        return None
    usage = payload["usage"]
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")
    details = usage.get("completion_tokens_details") or {}
    reasoning_tokens = details.get("reasoning_tokens", 0) if isinstance(details, dict) else 0
    values = (input_tokens, output_tokens, reasoning_tokens)
    if any(type(value) is not int or value < 0 for value in values):
        return None
    return values


def _error(status: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": "invalid_request_error", "code": code}},
    )


def _bearer(authorization: str | None, config: GatewayConfig) -> GatewayClaims:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        return verify_gateway_token(authorization[7:], config.auth_secret)
    except GatewayAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _sse(events: Iterator[dict]) -> Iterator[bytes]:
    for event in events:
        payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        yield f"event: {event['type']}\ndata: {payload}\n\n".encode("utf-8")
    yield b"data: [DONE]\n\n"


def create_app(
    config: GatewayConfig,
    *,
    http_client: httpx.AsyncClient | None = None,
    ledger: UsageLedger | None = None,
) -> FastAPI:
    app = FastAPI(title="Simverse Lab Model Gateway", docs_url=None, redoc_url=None)
    app.state.config = config
    app.state.http_client = http_client or httpx.AsyncClient(
        timeout=config.request_timeout_s, trust_env=False, follow_redirects=False
    )
    app.state.owns_http_client = http_client is None
    app.state.ledger = ledger or UsageLedger(config.ledger_path, config)
    app.state.owns_ledger = ledger is None
    app.state.reasoning = ReasoningStore(
        ttl_s=config.reasoning_ttl_s,
        max_entries=config.reasoning_max_entries,
    )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if app.state.owns_http_client:
            await app.state.http_client.aclose()
        if app.state.owns_ledger:
            app.state.ledger.close()

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict:
        return {"status": "ready", "models": ["deepseek-v4-flash", "deepseek-v4-pro"]}

    @app.get("/v1/models")
    async def models(authorization: str | None = Header(default=None)) -> dict:
        _bearer(authorization, config)
        return {"object": "list", "data": [{"id": "lab-auto", "object": "model"}]}

    @app.get("/v1/lab/usage")
    async def usage(authorization: str | None = Header(default=None)) -> dict:
        claims = _bearer(authorization, config)
        return app.state.ledger.get(claims.run_id, claims.model).as_dict()

    @app.post("/v1/lab/revoke")
    async def revoke(authorization: str | None = Header(default=None)) -> dict:
        claims = _bearer(authorization, config)
        return app.state.ledger.revoke(claims.run_id, claims.model).as_dict()

    @app.post("/v1/lab/renew")
    async def renew(authorization: str | None = Header(default=None)) -> dict:
        claims = _bearer(authorization, config)
        totals = app.state.ledger.get(claims.run_id, claims.model)
        if totals.revoked or totals.cost_unknown:
            return _error(401, "run grant is no longer renewable", "run_revoked")
        return {
            "token": renew_gateway_token(
                claims, config.auth_secret, config.token_renewal_ttl_s
            ),
            "expires_in_s": config.token_renewal_ttl_s,
        }

    @app.post("/v1/responses")
    async def responses(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        claims = _bearer(authorization, config)
        raw = await request.body()
        if len(raw) > config.max_request_bytes:
            return _error(413, "request exceeds the gateway byte limit", "request_too_large")
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error(400, "request body must be JSON", "invalid_json")
        if not isinstance(body, dict):
            return _error(400, "request body must be an object", "invalid_request")
        if body.get("model") != "lab-auto":
            return _error(403, "model selection is fixed by the run grant", "model_override_denied")
        if body.get("previous_response_id") is not None:
            return _error(400, "previous_response_id is not supported", "unsupported_state")

        requested_max = body.get("max_output_tokens")
        if type(requested_max) is not int or requested_max <= 0:
            requested_max = config.default_max_output_tokens
        max_output = min(requested_max, claims.max_model_tokens)
        try:
            chat_body, registry = responses_to_chat(
                body,
                model=claims.model,
                max_output_tokens=max_output,
                reasoning_for_call=lambda call_id: app.state.reasoning.get(
                    claims.run_id, call_id
                ),
            )
        except TranslationError as exc:
            return _error(400, str(exc), "unsupported_responses_shape")

        try:
            reservation_id = await asyncio.to_thread(
                app.state.ledger.reserve,
                run_id=claims.run_id,
                model=claims.model,
                estimated_input_tokens=_estimated_input_tokens(raw),
                max_output_tokens=max_output,
                max_model_tokens=claims.max_model_tokens,
                budget_usd_cents=claims.budget_usd_cents,
                max_inflight_requests=config.max_inflight_per_run,
            )
        except InflightLimitError:
            return _error(429, "run model request concurrency is exhausted", "inflight_exhausted")
        except RunRevokedError:
            return _error(401, "run token has been revoked", "run_revoked")
        except UsageUnknownError:
            return _error(503, "run model cost is unknown", "usage_unknown")
        except BudgetReservationError:
            return _error(402, "run model budget is exhausted", "budget_exhausted")

        try:
            upstream = await app.state.http_client.post(
                f"{config.upstream_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {config.upstream_api_key}",
                    "Content-Type": "application/json",
                },
                json=chat_body,
                timeout=config.request_timeout_s,
            )
        except httpx.TimeoutException:
            await asyncio.to_thread(app.state.ledger.mark_unknown, reservation_id)
            return _error(504, "model provider timed out", "upstream_timeout")
        except httpx.TransportError:
            await asyncio.to_thread(app.state.ledger.mark_unknown, reservation_id)
            return _error(502, "model provider is unavailable", "upstream_unavailable")
        if upstream.status_code >= 400:
            await asyncio.to_thread(app.state.ledger.release, reservation_id)
            status = 429 if upstream.status_code == 429 else 502
            return _error(status, "model provider rejected the request", "upstream_rejected")
        try:
            upstream_payload = upstream.json()
        except ValueError:
            await asyncio.to_thread(app.state.ledger.mark_unknown, reservation_id)
            return _error(502, "model provider returned unmetered output", "usage_unknown")
        usage = _upstream_usage(upstream_payload)
        if usage is None:
            await asyncio.to_thread(app.state.ledger.mark_unknown, reservation_id)
            return _error(502, "model provider returned unmetered output", "usage_unknown")
        settled = await asyncio.to_thread(
            app.state.ledger.settle,
            reservation_id,
            input_tokens=usage[0],
            output_tokens=usage[1],
            reasoning_tokens=usage[2],
        )
        if (
            settled.total_tokens > claims.max_model_tokens
            or settled.cost_usd_micros > claims.budget_usd_cents * 10_000
        ):
            await asyncio.to_thread(
                app.state.ledger.revoke, claims.run_id, claims.model
            )
            return _error(402, "upstream usage exceeded the run budget", "budget_exhausted")
        try:
            normalized, reasoning = chat_to_response(
                upstream_payload, public_model="lab-auto", tool_registry=registry
            )
        except (ValueError, TranslationError):
            return _error(502, "model provider returned an invalid response", "upstream_invalid")
        app.state.reasoning.put_many(claims.run_id, reasoning)
        if body.get("stream") is True:
            return StreamingResponse(_sse(response_events(normalized)), media_type="text/event-stream")
        return normalized

    @app.exception_handler(HTTPException)
    async def _http_exception(_request: Request, exc: HTTPException):
        return _error(exc.status_code, str(exc.detail), "authentication_failed")

    return app
