"""URL canonicalisation, host policy, and DNS-to-IP pinning for egress."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import quote, urlsplit, urlunsplit


class UnsafeEgressTarget(ValueError):
    code = "unsafe_egress_target"


@dataclass(frozen=True)
class ResolvedTarget:
    url: str
    scheme: str
    host: str
    port: int
    request_target: str
    addresses: tuple[str, ...]


def _public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(
        address.is_global
        and not address.is_private
        and not address.is_loopback
        and not address.is_link_local
        and not address.is_multicast
        and not address.is_reserved
        and not address.is_unspecified
    )


def normalize_http_url(raw_url: str, *, max_chars: int) -> str:
    if not isinstance(raw_url, str):
        raise UnsafeEgressTarget("URL must be a string")
    value = raw_url.strip()
    if not value or len(value) > max_chars:
        raise UnsafeEgressTarget("URL is empty or too long")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise UnsafeEgressTarget("URL contains control characters")
    if "\\" in value:
        raise UnsafeEgressTarget("URL contains a backslash")
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            raise UnsafeEgressTarget("only HTTP(S) URLs are allowed")
        if parsed.username is not None or parsed.password is not None:
            raise UnsafeEgressTarget("URL credentials are not allowed")
        raw_host = parsed.hostname
        if not raw_host:
            raise UnsafeEgressTarget("URL host is required")
        host = raw_host.rstrip(".").encode("idna").decode("ascii").lower()
        if not host or len(host) > 253:
            raise UnsafeEgressTarget("URL host is invalid")
        port = parsed.port
    except (UnicodeError, ValueError) as exc:
        if isinstance(exc, UnsafeEgressTarget):
            raise
        raise UnsafeEgressTarget("URL is malformed") from exc

    default_port = 443 if scheme == "https" else 80
    effective_port = port or default_port
    host_display = f"[{host}]" if ":" in host else host
    netloc = host_display if effective_port == default_port else f"{host_display}:{effective_port}"
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="=&;%:@!$'()*+,/?-._~")
    return urlunsplit((scheme, netloc, path, query, ""))


def host_allowed(host: str, allowlist: list[str] | tuple[str, ...]) -> bool:
    host = host.rstrip(".").lower()
    for raw_entry in allowlist:
        entry = raw_entry.strip().lower().rstrip(".")
        if not entry or "://" in entry or "/" in entry or "@" in entry:
            continue
        if entry.startswith("*."):
            suffix = entry[2:]
            if suffix and (host == suffix or host.endswith(f".{suffix}")):
                return True
        elif host == entry:
            return True
    return False


async def resolve_target(
    raw_url: str,
    *,
    allowlist: list[str] | tuple[str, ...],
    allowed_ports: tuple[int, ...],
    max_chars: int,
) -> ResolvedTarget:
    normalized = normalize_http_url(raw_url, max_chars=max_chars)
    parsed = urlsplit(normalized)
    assert parsed.hostname is not None
    host = parsed.hostname.rstrip(".").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if port not in allowed_ports:
        raise UnsafeEgressTarget("egress port is not allowed")
    if not host_allowed(host, allowlist):
        raise UnsafeEgressTarget("egress host is not allowed")

    if _public_ip(host):
        addresses = (host,)
    else:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as exc:
            raise UnsafeEgressTarget("egress host cannot be resolved") from exc
        addresses = tuple(dict.fromkeys(str(info[4][0]).split("%", 1)[0] for info in infos))
        if not addresses:
            raise UnsafeEgressTarget("egress host has no addresses")
        # Reject a mixed public/private answer rather than choosing the safe half.
        # The exact public address selected below is then used for the socket, so
        # DNS cannot be rebound between validation and connect.
        if any(not _public_ip(address) for address in addresses):
            raise UnsafeEgressTarget("egress host resolved to a non-public address")

    target = parsed.path or "/"
    if parsed.query:
        target = f"{target}?{parsed.query}"
    return ResolvedTarget(
        url=normalized,
        scheme=parsed.scheme,
        host=host,
        port=port,
        request_target=target,
        addresses=addresses,
    )
