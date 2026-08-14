"""Small pure-ASGI request-body limiter for upload endpoints.

Route handlers run only after FastAPI has parsed multipart data, so limiting an
``UploadFile`` there is too late to bound how much the server receives/spools.
This middleware checks both the declared and the actual streamed body size
before the multipart parser can consume an unbounded request.
"""
from __future__ import annotations

from starlette.responses import JSONResponse


class RequestBodyTooLarge(Exception):
    """Internal control-flow signal raised by the bounded ASGI receiver."""


class RouteBodyLimitMiddleware:
    """Apply byte limits to selected ``(METHOD, path)`` pairs.

    ``Content-Length`` provides an immediate rejection when present, while the
    receive wrapper remains authoritative for chunked requests and clients that
    under-report the header.
    """

    def __init__(
        self,
        app,
        *,
        limits: dict[tuple[str, str], int],
        detail: str = "Request body exceeds the size limit",
    ) -> None:
        self.app = app
        self.limits = {
            (method.upper(), path): int(limit)
            for (method, path), limit in limits.items()
        }
        self.detail = detail

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        key = (scope.get("method", "").upper(), scope.get("path", ""))
        limit = self.limits.get(key)
        if limit is None:
            await self.app(scope, receive, send)
            return

        content_lengths = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == b"content-length"
        ]
        if content_lengths:
            try:
                declared_values = {int(value.decode("ascii")) for value in content_lengths}
            except (UnicodeDecodeError, ValueError):
                declared_values = set()
            # Conflicting/malformed lengths are not trustworthy.  The HTTP
            # server normally rejects them first, but keep the app boundary
            # deterministic for direct ASGI clients as well.
            if len(declared_values) != 1 or next(iter(declared_values), -1) < 0:
                await JSONResponse(
                    {"detail": "Invalid Content-Length"}, status_code=400
                )(scope, receive, send)
                return
            if next(iter(declared_values)) > limit:
                await JSONResponse({"detail": self.detail}, status_code=413)(
                    scope, receive, send
                )
                return

        consumed = 0
        limit_exceeded = False
        response_started = False

        async def limited_receive():
            nonlocal consumed, limit_exceeded
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    limit_exceeded = True
                    raise RequestBodyTooLarge
            return message

        async def tracked_send(message) -> None:
            nonlocal response_started
            # Starlette's multipart parser translates receive errors into its
            # own 400 response.  Once our receiver has observed the real byte
            # overflow, suppress that downstream response so the boundary can
            # return the authoritative 413 after unwinding.
            if limit_exceeded:
                return
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except RequestBodyTooLarge:
            pass
        if limit_exceeded:
            # Multipart parsing completes before the endpoint starts a
            # response. Avoid a second response if a future streaming endpoint
            # reuses this middleware and consumes request data after starting.
            if response_started:
                raise RequestBodyTooLarge
            await JSONResponse({"detail": self.detail}, status_code=413)(
                scope, receive, send
            )
