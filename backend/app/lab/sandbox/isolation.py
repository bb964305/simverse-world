"""One-shot isolation封装 for real sandbox runs (spec §5.3, §11).

Describes the container/network/filesystem guarantees a real adapter must run
under: a short-lived non-root container with a read-only rootfs + a quota'd
scratch dir, and a default-deny egress firewall that only allows the run's
``egress_allowlist`` while blocking internal/metadata addresses (anti-SSRF).

Import-safe with no external deps: this module only *builds the spec* and does
SSRF-style host screening. Actually launching a container is the deployment's
container runtime (docker/gVisor/firecracker) wired in via ``lab_sandbox_image``
— left as an integration point so P2 imports and unit-tests without a daemon.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from urllib.parse import urlparse

# Link-local / metadata / loopback ranges an egress target must never resolve to.
_BLOCKED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local incl. 169.254.169.254 metadata
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


@dataclass
class IsolationSpec:
    image: str
    egress_allowlist: list[str] = field(default_factory=list)
    read_only_rootfs: bool = True
    run_as_non_root: bool = True
    scratch_quota_mb: int = 512
    network_default_deny: bool = True
    drop_all_capabilities: bool = True


def build_isolation_spec(image: str, egress_allowlist: list[str]) -> IsolationSpec:
    return IsolationSpec(image=image, egress_allowlist=list(egress_allowlist or []))


def is_host_blocked(host: str) -> bool:
    """True if a literal IP host falls in a blocked (internal/metadata) range.
    Hostnames (non-literal) are screened at connect time by the egress firewall;
    here we only catch raw-IP SSRF attempts."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(ip in net for net in _BLOCKED_NETS)


def is_egress_allowed(url: str, allowlist: list[str]) -> bool:
    """Default-deny: a URL is allowed only if its host matches an allowlist entry
    (exact or suffix wildcard like ``*.wikipedia.org``) and is not an internal IP."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    if not host or is_host_blocked(host):
        return False
    for entry in allowlist or []:
        entry = entry.lower().strip()
        if entry.startswith("*."):
            if host == entry[2:] or host.endswith(entry[1:]):
                return True
        elif host == entry:
            return True
    return False
