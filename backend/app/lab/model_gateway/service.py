from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.lab.model_gateway.auth import (
    GatewayAuthError,
    GatewayClaims,
    verify_gateway_token,
)
from app.lab.model_gateway.config import GatewayConfig
from app.lab.model_gateway.ledger import UsageLedger
from app.lab.model_gateway.translation import (
    TranslationError,
    chat_to_response,
    response_events,
    responses_to_chat,
)


class ReasoningStore:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, call_id: str) -> str | None:
        with self._lock:
            return self._values.get(call_id)

    def put_many(self, values: dict[str, str]) -> None:
        with self._lock:
            self._values.update(values)


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
    app.state.reasoning = ReasoningStore()

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

        totals = app.state.ledger.get(claims.run_id, claims.model)
        if totals.total_tokens >= claims.max_model_tokens:
            return _error(402, "run model-token budget is exhausted", "budget_exhausted")
        if totals.cost_usd_micros >= claims.budget_usd_cents * 10_000:
            return _error(402, "run model-cost budget is exhausted", "budget_exhausted")
        remaining = claims.max_model_tokens - totals.total_tokens
        requested_max = body.get("max_output_tokens")
        if type(requested_max) is not int or requested_max <= 0:
            requested_max = config.default_max_output_tokens
        max_output = min(requested_max, remaining)
        try:
            chat_body, registry = responses_to_chat(
                body,
                model=claims.model,
                max_output_tokens=max_output,
                reasoning_for_call=app.state.reasoning.get,
            )
        except TranslationError as exc:
            return _error(400, str(exc), "unsupported_responses_shape")

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
            return _error(504, "model provider timed out", "upstream_timeout")
        except httpx.TransportError:
            return _error(502, "model provider is unavailable", "upstream_unavailable")
        if upstream.status_code >= 400:
            status = 429 if upstream.status_code == 429 else 502
            return _error(status, "model provider rejected the request", "upstream_rejected")
        try:
            normalized, reasoning = chat_to_response(
                upstream.json(), public_model="lab-auto", tool_registry=registry
            )
        except (ValueError, TranslationError):
            return _error(502, "model provider returned an invalid response", "upstream_invalid")
        app.state.reasoning.put_many(reasoning)
        response_usage = normalized["usage"]
        await asyncio.to_thread(
            app.state.ledger.record,
            run_id=claims.run_id,
            model=claims.model,
            input_tokens=response_usage["input_tokens"],
            output_tokens=response_usage["output_tokens"],
            reasoning_tokens=response_usage["output_tokens_details"]["reasoning_tokens"],
        )
        if body.get("stream") is True:
            return StreamingResponse(_sse(response_events(normalized)), media_type="text/event-stream")
        return normalized

    @app.exception_handler(HTTPException)
    async def _http_exception(_request: Request, exc: HTTPException):
        return _error(exc.status_code, str(exc.detail), "authentication_failed")

    return app
