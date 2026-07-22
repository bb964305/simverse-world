"""Environment contract for the independently deployed Lab egress service.

The project settings object intentionally does not own these values yet: the
egress process is an independent trust boundary and can be deployed before its
Compose wiring lands.  All defaults are fail-closed or tightly bounded.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib.parse import urlsplit


class EgressConfigurationError(ValueError):
    pass


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _integer(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise EgressConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise EgressConfigurationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def _float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise EgressConfigurationError(f"{name} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise EgressConfigurationError(
            f"{name} must be between {minimum} and {maximum}"
        )
    return value


def _ports() -> tuple[int, ...]:
    raw = os.getenv("LAB_EGRESS_ALLOWED_PORTS", "80,443").strip()
    values: object
    if raw.startswith("["):
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EgressConfigurationError(
                "LAB_EGRESS_ALLOWED_PORTS must be CSV or a JSON list"
            ) from exc
    else:
        values = [item.strip() for item in raw.split(",") if item.strip()]
    if not isinstance(values, list) or not values:
        raise EgressConfigurationError("LAB_EGRESS_ALLOWED_PORTS cannot be empty")
    ports: set[int] = set()
    for item in values:
        try:
            port = int(item)
        except (TypeError, ValueError) as exc:
            raise EgressConfigurationError("invalid egress port") from exc
        if not 1 <= port <= 65535:
            raise EgressConfigurationError("egress ports must be between 1 and 65535")
        ports.add(port)
    return tuple(sorted(ports))


@dataclass(frozen=True)
class EgressConfig:
    enabled: bool
    api_key: str
    database_path: str
    search_endpoint: str
    search_provider: str
    allowed_ports: tuple[int, ...]
    connect_timeout_s: float
    read_timeout_s: float
    total_timeout_s: float
    action_lease_s: int
    max_attempts: int
    max_redirects: int
    max_response_bytes: int
    max_header_bytes: int
    max_text_chars: int
    max_links: int
    max_search_results: int
    max_query_chars: int
    max_url_chars: int
    user_agent: str

    @property
    def search_available(self) -> bool:
        return bool(self.enabled and self.search_endpoint)

    @property
    def fetch_available(self) -> bool:
        return self.enabled

    def validate_service(self) -> None:
        if not self.enabled:
            raise EgressConfigurationError("LAB_EGRESS_ENABLED must be true")
        if len(self.api_key) < 32:
            raise EgressConfigurationError(
                "LAB_EGRESS_API_KEY must contain at least 32 characters"
            )
        if not os.path.isabs(self.database_path):
            raise EgressConfigurationError("LAB_EGRESS_DATABASE_PATH must be absolute")
        if self.search_provider != "searxng_json":
            raise EgressConfigurationError(
                "LAB_EGRESS_SEARCH_PROVIDER must be searxng_json"
            )
        if self.search_endpoint:
            parsed = urlsplit(self.search_endpoint)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise EgressConfigurationError(
                    "LAB_EGRESS_SEARCH_ENDPOINT must be an absolute HTTP(S) URL"
                )
            if parsed.username is not None or parsed.password is not None:
                raise EgressConfigurationError(
                    "LAB_EGRESS_SEARCH_ENDPOINT cannot contain credentials"
                )
        if self.action_lease_s <= self.total_timeout_s:
            raise EgressConfigurationError(
                "LAB_EGRESS_ACTION_LEASE_S must exceed LAB_EGRESS_TOTAL_TIMEOUT_S"
            )
        if any(ord(char) < 0x20 or ord(char) > 0x7E for char in self.user_agent):
            raise EgressConfigurationError(
                "LAB_EGRESS_USER_AGENT must contain visible ASCII only"
            )


def load_egress_config() -> EgressConfig:
    return EgressConfig(
        enabled=_enabled(os.getenv("LAB_EGRESS_ENABLED")),
        api_key=os.getenv("LAB_EGRESS_API_KEY", ""),
        database_path=os.getenv(
            "LAB_EGRESS_DATABASE_PATH", "/var/lib/simverse-lab-egress/actions.sqlite3"
        ),
        search_endpoint=os.getenv("LAB_EGRESS_SEARCH_ENDPOINT", "").strip(),
        search_provider=os.getenv(
            "LAB_EGRESS_SEARCH_PROVIDER", "searxng_json"
        ).strip(),
        allowed_ports=_ports(),
        connect_timeout_s=_float(
            "LAB_EGRESS_CONNECT_TIMEOUT_S", 5.0, minimum=0.1, maximum=30.0
        ),
        read_timeout_s=_float(
            "LAB_EGRESS_READ_TIMEOUT_S", 10.0, minimum=0.1, maximum=60.0
        ),
        total_timeout_s=_float(
            "LAB_EGRESS_TOTAL_TIMEOUT_S", 20.0, minimum=0.5, maximum=120.0
        ),
        action_lease_s=_integer(
            "LAB_EGRESS_ACTION_LEASE_S", 45, minimum=2, maximum=600
        ),
        max_attempts=_integer(
            "LAB_EGRESS_MAX_ATTEMPTS", 3, minimum=1, maximum=10
        ),
        max_redirects=_integer(
            "LAB_EGRESS_MAX_REDIRECTS", 3, minimum=0, maximum=10
        ),
        max_response_bytes=_integer(
            "LAB_EGRESS_MAX_RESPONSE_BYTES",
            1_048_576,
            minimum=1_024,
            maximum=10_485_760,
        ),
        max_header_bytes=_integer(
            "LAB_EGRESS_MAX_HEADER_BYTES", 65_536, minimum=4_096, maximum=262_144
        ),
        max_text_chars=_integer(
            "LAB_EGRESS_MAX_TEXT_CHARS", 32_768, minimum=1_024, maximum=131_072
        ),
        max_links=_integer(
            "LAB_EGRESS_MAX_LINKS", 50, minimum=1, maximum=500
        ),
        max_search_results=_integer(
            "LAB_EGRESS_MAX_SEARCH_RESULTS", 10, minimum=1, maximum=50
        ),
        max_query_chars=_integer(
            "LAB_EGRESS_MAX_QUERY_CHARS", 512, minimum=16, maximum=2_048
        ),
        max_url_chars=_integer(
            "LAB_EGRESS_MAX_URL_CHARS", 2_048, minimum=256, maximum=8_192
        ),
        user_agent=os.getenv(
            "LAB_EGRESS_USER_AGENT", "SimverseLabEgress/1.0"
        ).strip()[:128]
        or "SimverseLabEgress/1.0",
    )


def configured_search_target() -> str:
    """Return the configured search network target without inventing a fallback."""

    config = load_egress_config()
    return config.search_endpoint if config.search_available else ""


def configured_runtime_tools() -> frozenset[str]:
    """Tools the Runtime may publish for this deployment.

    This is intentionally configuration driven.  Merely importing handler code
    must not make a Runtime advertise a network capability.
    """

    config = load_egress_config()
    if not config.enabled:
        return frozenset()
    tools = {"web.fetch", "browser.navigate"}
    if config.search_endpoint:
        tools.add("web.search")
    return frozenset(tools)


def configured_runner_tools() -> frozenset[str]:
    """Handlers for which the Runner has an authenticated service route."""

    base_url = os.getenv("LAB_EGRESS_BASE_URL", "").strip()
    api_key = os.getenv("LAB_EGRESS_API_KEY", "")
    if not base_url or len(api_key) < 32:
        return frozenset()
    try:
        parsed = urlsplit(base_url)
    except ValueError:
        return frozenset()
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return frozenset()
    return configured_runtime_tools()
