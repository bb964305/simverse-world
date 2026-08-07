from __future__ import annotations

import asyncio
import hmac

import httpx


_MAX_HEADER_BYTES = 64 * 1024
_MAX_BODY_BYTES = 2 * 1024 * 1024


class RunCredentialProxy:
    """Per-run loopback proxy that keeps the real gateway token out of Codex."""

    def __init__(self, *, gateway_base_url: str, gateway_token: str, client_token: str):
        self.gateway_base_url = gateway_base_url.rstrip("/")
        self.gateway_token = gateway_token
        self.client_token = client_token
        self._server: asyncio.AbstractServer | None = None
        self._client = httpx.AsyncClient(timeout=620, trust_env=False)
        self._renew_task: asyncio.Task | None = None

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self._renew_task = asyncio.create_task(self._renew_loop())
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}/v1"

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        if self._renew_task is not None:
            self._renew_task.cancel()
            await asyncio.gather(self._renew_task, return_exceptions=True)
        await self._client.aclose()
        self.gateway_token = ""
        self.client_token = ""

    async def _renew_loop(self) -> None:
        while True:
            await asyncio.sleep(120)
            response = await self._client.post(
                self.gateway_base_url + "/lab/renew",
                headers={"Authorization": f"Bearer {self.gateway_token}"},
            )
            response.raise_for_status()
            payload = response.json()
            token = payload.get("token") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not token:
                raise RuntimeError("gateway returned an invalid renewed token")
            self.gateway_token = token

    def check_healthy(self) -> None:
        if self._renew_task is not None and self._renew_task.done():
            error = self._renew_task.exception()
            if error is not None:
                raise RuntimeError("gateway token renewal failed") from error

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            header_block = await reader.readuntil(b"\r\n\r\n")
            if len(header_block) > _MAX_HEADER_BYTES:
                raise ValueError("headers too large")
            lines = header_block.decode("latin-1").split("\r\n")
            method, path, _version = lines[0].split(" ", 2)
            allowed = {
                ("GET", "/v1/models"),
                ("POST", "/v1/responses"),
            }
            if (method, path) not in allowed:
                await self._reply(writer, 403, b"request denied", "text/plain")
                return
            headers: dict[str, str] = {}
            for line in lines[1:]:
                if not line:
                    continue
                name, value = line.split(":", 1)
                headers[name.lower()] = value.strip()
            expected = f"Bearer {self.client_token}"
            if not hmac.compare_digest(headers.get("authorization", ""), expected):
                await self._reply(writer, 401, b"unauthorized", "text/plain")
                return
            content_length = int(headers.get("content-length", "0"))
            if content_length < 0 or content_length > _MAX_BODY_BYTES:
                await self._reply(writer, 413, b"request too large", "text/plain")
                return
            body = await reader.readexactly(content_length) if content_length else b""
            suffix = path.removeprefix("/v1")
            response = await self._client.request(
                method,
                self.gateway_base_url + suffix,
                headers={
                    "Authorization": f"Bearer {self.gateway_token}",
                    "Content-Type": headers.get("content-type", "application/json"),
                },
                content=body,
            )
            await self._reply(
                writer,
                response.status_code,
                response.content,
                response.headers.get("content-type", "application/json"),
            )
        except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            await self._reply(writer, 400, b"invalid request", "text/plain")
        except Exception:
            await self._reply(writer, 502, b"gateway unavailable", "text/plain")
        finally:
            writer.close()
            await writer.wait_closed()

    @staticmethod
    async def _reply(
        writer: asyncio.StreamWriter, status: int, body: bytes, content_type: str
    ) -> None:
        reason = {
            200: "OK", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 413: "Content Too Large", 502: "Bad Gateway",
        }.get(status, "Upstream Response")
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n".encode("latin-1") + body
        )
        await writer.drain()
