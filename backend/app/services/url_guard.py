"""Outbound URL guard against SSRF (P0-4d).

User- or admin-supplied base URLs (custom LLM endpoint, portrait endpoint,
connection tests) trigger server-side requests. Without validation these can
probe internal networks or cloud metadata (e.g. 169.254.169.254).

`ensure_url_is_public` resolves the target host and rejects any address that
is not globally routable. In debug mode the IP-range check is skipped so
local development against localhost/Ollama keeps working; syntactic checks
(scheme, host present) always apply.

Limitation: resolution happens at validation time, so a DNS-rebinding
attacker could still swap the record before the actual request. Full
protection requires a pinned-IP transport; out of scope for P0-4d.
"""
import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from app.config import settings


class UnsafeURLError(ValueError):
    """Raised when a URL targets a non-public or malformed destination."""


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return ip.is_global and not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def ensure_url_is_public(url: str) -> None:
    """Validate that `url` is http(s) and resolves only to public addresses.

    Raises UnsafeURLError on any violation. In debug mode only the
    syntactic checks apply (scheme + host), not the IP-range check.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"URL scheme must be http or https, got {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError("URL has no host")

    if settings.debug:
        return

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, type=socket.SOCK_STREAM
        )
    except socket.gaierror as e:
        raise UnsafeURLError(f"Cannot resolve host {host!r}") from e

    for info in infos:
        # sockaddr[0] is the address string; IPv6 may carry a %scope suffix
        addr = str(info[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError as e:
            raise UnsafeURLError(f"Unparseable address for host {host!r}") from e
        if not _is_public(ip):
            raise UnsafeURLError(
                f"Host {host!r} resolves to non-public address {addr} — refusing request"
            )
