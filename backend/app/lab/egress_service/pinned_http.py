"""Minimal HTTP/1.1 client that connects to a validated, pinned IP address.

Using a normal URL client after a separate DNS check leaves a DNS-rebinding
window.  This transport resolves and validates every answer, then passes the
chosen numeric address to ``asyncio.open_connection`` while retaining the
original hostname for TLS SNI/certificate verification and the Host header.
Redirects are deliberately handled by the caller and therefore re-enter the
same validation path one hop at a time.
"""
from __future__ import annotations

import asyncio
import ssl
import zlib
from dataclasses import dataclass
from urllib.parse import urljoin

from .config import EgressConfig
from .models import EgressUsage
from .security import ResolvedTarget, UnsafeEgressTarget, resolve_target

_REDIRECTS = frozenset({301, 302, 303, 307, 308})


class EgressFetchError(RuntimeError):
    def __init__(self, code: str, usage: EgressUsage, message: str | None = None):
        self.code = code
        self.usage = usage
        super().__init__(message or code)


@dataclass(frozen=True)
class FetchResponse:
    url: str
    status_code: int
    headers: dict[str, str]
    body: bytes
    history: tuple[str, ...]
    usage: EgressUsage


class PinnedHttpClient:
    def __init__(self, config: EgressConfig):
        self.config = config
        self._ssl_context = ssl.create_default_context()
        self._ssl_context.set_alpn_protocols(["http/1.1"])

    async def get(
        self, raw_url: str, *, allowlist: list[str] | tuple[str, ...]
    ) -> FetchResponse:
        counters = {"requests": 0, "bytes": 0}
        history: list[str] = []
        current_url = raw_url
        consumed_body_bytes = 0
        try:
            async with asyncio.timeout(self.config.total_timeout_s):
                for redirect_count in range(self.config.max_redirects + 1):
                    target = await resolve_target(
                        current_url,
                        allowlist=allowlist,
                        allowed_ports=self.config.allowed_ports,
                        max_chars=self.config.max_url_chars,
                    )
                    response = await self._request_once(
                        target,
                        counters=counters,
                        max_body_bytes=(
                            self.config.max_response_bytes - consumed_body_bytes
                        ),
                    )
                    status_code, headers, body = response
                    consumed_body_bytes += len(body)
                    if status_code not in _REDIRECTS:
                        return FetchResponse(
                            url=target.url,
                            status_code=status_code,
                            headers=headers,
                            body=self._decode_content(body, headers, counters),
                            history=tuple(history),
                            usage=EgressUsage(**counters),
                        )
                    location = headers.get("location", "").strip()
                    if not location:
                        return FetchResponse(
                            url=target.url,
                            status_code=status_code,
                            headers=headers,
                            body=self._decode_content(body, headers, counters),
                            history=tuple(history),
                            usage=EgressUsage(**counters),
                        )
                    if redirect_count >= self.config.max_redirects:
                        raise EgressFetchError(
                            "redirect_limit_exceeded", EgressUsage(**counters)
                        )
                    history.append(target.url)
                    current_url = urljoin(target.url, location)
        except EgressFetchError:
            raise
        except UnsafeEgressTarget as exc:
            raise EgressFetchError(
                exc.code, EgressUsage(**counters), str(exc)
            ) from exc
        except TimeoutError as exc:
            raise EgressFetchError(
                "egress_timeout", EgressUsage(**counters)
            ) from exc
        except (OSError, ssl.SSLError, asyncio.IncompleteReadError) as exc:
            raise EgressFetchError(
                "egress_transport_error", EgressUsage(**counters)
            ) from exc
        raise EgressFetchError("redirect_limit_exceeded", EgressUsage(**counters))

    async def _request_once(
        self,
        target: ResolvedTarget,
        *,
        counters: dict[str, int],
        max_body_bytes: int,
    ) -> tuple[int, dict[str, str], bytes]:
        reader: asyncio.StreamReader | None = None
        writer: asyncio.StreamWriter | None = None
        last_error: BaseException | None = None
        ssl_context = self._ssl_context if target.scheme == "https" else None
        for address in target.addresses:
            try:
                kwargs = {
                    "host": address,
                    "port": target.port,
                    "ssl": ssl_context,
                    "limit": self.config.max_header_bytes + 1,
                }
                if ssl_context is not None:
                    kwargs.update(
                        server_hostname=target.host,
                        ssl_handshake_timeout=self.config.connect_timeout_s,
                    )
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(**kwargs),
                    timeout=self.config.connect_timeout_s,
                )
                break
            except (OSError, ssl.SSLError, TimeoutError) as exc:
                last_error = exc
        if reader is None or writer is None:
            raise EgressFetchError(
                "egress_connect_failed", EgressUsage(**counters)
            ) from last_error

        default_port = 443 if target.scheme == "https" else 80
        host_value = f"[{target.host}]" if ":" in target.host else target.host
        if target.port != default_port:
            host_value = f"{host_value}:{target.port}"
        request = (
            f"GET {target.request_target} HTTP/1.1\r\n"
            f"Host: {host_value}\r\n"
            f"User-Agent: {self.config.user_agent}\r\n"
            "Accept: text/html,application/json,text/plain;q=0.9,*/*;q=0.1\r\n"
            "Accept-Encoding: gzip, deflate, identity\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        try:
            writer.write(request)
            await asyncio.wait_for(writer.drain(), timeout=self.config.connect_timeout_s)
            counters["requests"] += 1
            try:
                header_blob = await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"),
                    timeout=self.config.read_timeout_s,
                )
            except (asyncio.LimitOverrunError, asyncio.IncompleteReadError) as exc:
                raise EgressFetchError(
                    "invalid_http_headers", EgressUsage(**counters)
                ) from exc
            if len(header_blob) > self.config.max_header_bytes:
                raise EgressFetchError(
                    "response_headers_too_large", EgressUsage(**counters)
                )
            counters["bytes"] += len(header_blob)
            status_code, headers = self._parse_headers(header_blob, counters)
            body = await self._read_body(
                reader,
                headers=headers,
                status_code=status_code,
                counters=counters,
                max_bytes=max_body_bytes,
            )
            return status_code, headers, body
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ssl.SSLError):
                pass

    @staticmethod
    def _parse_headers(
        blob: bytes, counters: dict[str, int]
    ) -> tuple[int, dict[str, str]]:
        try:
            lines = blob[:-4].split(b"\r\n")
            status_parts = lines[0].decode("ascii").split(" ", 2)
            if len(status_parts) < 2 or not status_parts[0].startswith("HTTP/1."):
                raise ValueError
            status_code = int(status_parts[1])
            if not 100 <= status_code <= 599 or 100 <= status_code < 200:
                raise ValueError
            headers: dict[str, str] = {}
            content_length_seen = False
            for raw_line in lines[1:]:
                if not raw_line or raw_line[:1] in {b" ", b"\t"} or b":" not in raw_line:
                    raise ValueError
                raw_name, raw_value = raw_line.split(b":", 1)
                name = raw_name.decode("ascii").strip().lower()
                value = raw_value.decode("latin-1").strip()
                if not name or any(not (char.isalnum() or char in "!#$%&'*+-.^_`|~") for char in name):
                    raise ValueError
                if "\r" in value or "\n" in value:
                    raise ValueError
                if name == "content-length":
                    if content_length_seen:
                        raise ValueError
                    content_length_seen = True
                headers[name] = f"{headers[name]}, {value}" if name in headers else value
            return status_code, headers
        except (UnicodeError, ValueError, IndexError) as exc:
            raise EgressFetchError(
                "invalid_http_headers", EgressUsage(**counters)
            ) from exc

    async def _read_body(
        self,
        reader: asyncio.StreamReader,
        *,
        headers: dict[str, str],
        status_code: int,
        counters: dict[str, int],
        max_bytes: int,
    ) -> bytes:
        if status_code in {204, 304}:
            return b""
        transfer_encoding = headers.get("transfer-encoding", "").lower()
        if transfer_encoding and "content-length" in headers:
            raise EgressFetchError(
                "ambiguous_response_framing", EgressUsage(**counters)
            )
        if transfer_encoding:
            codings = [part.strip() for part in transfer_encoding.split(",")]
            if codings != ["chunked"]:
                raise EgressFetchError(
                    "unsupported_transfer_encoding", EgressUsage(**counters)
                )
            return await self._read_chunked(
                reader, counters=counters, max_bytes=max_bytes
            )

        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                length = int(content_length)
            except ValueError as exc:
                raise EgressFetchError(
                    "invalid_content_length", EgressUsage(**counters)
                ) from exc
            if length < 0 or length > max_bytes:
                raise EgressFetchError(
                    "response_too_large", EgressUsage(**counters)
                )
            body = await asyncio.wait_for(
                reader.readexactly(length), timeout=self.config.read_timeout_s
            )
            counters["bytes"] += len(body)
            return body

        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = await asyncio.wait_for(
                reader.read(min(65_536, max_bytes - size + 1)),
                timeout=self.config.read_timeout_s,
            )
            if not chunk:
                break
            size += len(chunk)
            counters["bytes"] += len(chunk)
            if size > max_bytes:
                raise EgressFetchError(
                    "response_too_large", EgressUsage(**counters)
                )
            chunks.append(chunk)
        return b"".join(chunks)

    async def _read_chunked(
        self,
        reader: asyncio.StreamReader,
        *,
        counters: dict[str, int],
        max_bytes: int,
    ) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            line = await asyncio.wait_for(
                reader.readline(), timeout=self.config.read_timeout_s
            )
            counters["bytes"] += len(line)
            if not line.endswith(b"\r\n") or len(line) > 128:
                raise EgressFetchError(
                    "invalid_chunked_body", EgressUsage(**counters)
                )
            try:
                chunk_size = int(line[:-2].split(b";", 1)[0], 16)
            except ValueError as exc:
                raise EgressFetchError(
                    "invalid_chunked_body", EgressUsage(**counters)
                ) from exc
            if chunk_size == 0:
                # Bound trailer parsing by the same header limit and discard it.
                trailer_bytes = 0
                while True:
                    trailer = await asyncio.wait_for(
                        reader.readline(), timeout=self.config.read_timeout_s
                    )
                    trailer_bytes += len(trailer)
                    counters["bytes"] += len(trailer)
                    if trailer == b"\r\n":
                        return b"".join(chunks)
                    if not trailer.endswith(b"\r\n") or trailer_bytes > self.config.max_header_bytes:
                        raise EgressFetchError(
                            "invalid_chunked_trailer", EgressUsage(**counters)
                        )
            if chunk_size < 0 or size + chunk_size > max_bytes:
                raise EgressFetchError(
                    "response_too_large", EgressUsage(**counters)
                )
            data = await asyncio.wait_for(
                reader.readexactly(chunk_size + 2),
                timeout=self.config.read_timeout_s,
            )
            counters["bytes"] += len(data)
            if not data.endswith(b"\r\n"):
                raise EgressFetchError(
                    "invalid_chunked_body", EgressUsage(**counters)
                )
            chunks.append(data[:-2])
            size += chunk_size

    def _decode_content(
        self, body: bytes, headers: dict[str, str], counters: dict[str, int]
    ) -> bytes:
        encoding = headers.get("content-encoding", "identity").strip().lower()
        if encoding in {"", "identity"}:
            return body
        if encoding not in {"gzip", "deflate"}:
            raise EgressFetchError(
                "unsupported_content_encoding", EgressUsage(**counters)
            )
        wbits = 16 + zlib.MAX_WBITS if encoding == "gzip" else zlib.MAX_WBITS
        try:
            decoder = zlib.decompressobj(wbits)
            output = decoder.decompress(body, self.config.max_response_bytes + 1)
            if (
                decoder.unconsumed_tail
                or len(output) > self.config.max_response_bytes
            ):
                raise EgressFetchError(
                    "response_too_large", EgressUsage(**counters)
                )
            output += decoder.flush(self.config.max_response_bytes - len(output) + 1)
        except zlib.error as exc:
            raise EgressFetchError(
                "invalid_content_encoding", EgressUsage(**counters)
            ) from exc
        if decoder.unconsumed_tail or len(output) > self.config.max_response_bytes:
            raise EgressFetchError(
                "response_too_large", EgressUsage(**counters)
            )
        if not decoder.eof or decoder.unused_data:
            raise EgressFetchError(
                "invalid_content_encoding", EgressUsage(**counters)
            )
        return output
