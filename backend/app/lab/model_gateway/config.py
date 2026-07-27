from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping
from urllib.parse import urlparse

from app.lab.model_catalog import FLASH_MODEL, PRO_MODEL


def _required(env: Mapping[str, str], primary: str, fallback: str | None = None) -> str:
    value = env.get(primary, "")
    if not value and fallback:
        value = env.get(fallback, "")
    if not value or value != value.strip():
        raise ValueError(f"{primary} is required and must be canonical")
    return value


def _decimal(env: Mapping[str, str], name: str, default: str) -> Decimal:
    try:
        value = Decimal(env.get(name, default))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _upstream_base_url(value: str) -> str:
    value = value.rstrip("/")
    if value.endswith("/apps/anthropic"):
        value = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.netloc != "dashscope.aliyuncs.com":
        raise ValueError(
            "model gateway upstream must be the HTTPS DashScope compatible endpoint"
        )
    if not parsed.path.endswith("/compatible-mode/v1"):
        raise ValueError("model gateway upstream must end in /compatible-mode/v1")
    return value


@dataclass(frozen=True)
class GatewayConfig:
    bind_host: str
    bind_port: int
    upstream_base_url: str
    upstream_api_key: str
    auth_secret: str
    ledger_path: str
    request_timeout_s: float
    max_request_bytes: int
    default_max_output_tokens: int
    cny_per_usd: Decimal
    flash_input_cny_per_million: Decimal
    flash_output_cny_per_million: Decimal
    pro_input_cny_per_million: Decimal
    pro_output_cny_per_million: Decimal
    max_inflight_per_run: int = 2
    reasoning_ttl_s: int = 900
    reasoning_max_entries: int = 4096
    token_renewal_ttl_s: int = 300

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "GatewayConfig":
        env = os.environ if environ is None else environ
        secret = _required(
            env, "LAB_MODEL_GATEWAY_AUTH_SECRET", "LAB_MODEL_GATEWAY_AUTH_SECRET"
        )
        if len(secret.encode("utf-8")) < 32:
            raise ValueError("LAB_MODEL_GATEWAY_AUTH_SECRET must be at least 32 bytes")
        try:
            port = int(env.get("LAB_MODEL_GATEWAY_BIND_PORT", "8096"))
            max_bytes = int(env.get("LAB_MODEL_GATEWAY_MAX_REQUEST_BYTES", "1048576"))
            max_output = int(env.get("LAB_MODEL_GATEWAY_DEFAULT_MAX_OUTPUT_TOKENS", "8192"))
            timeout = float(env.get("LAB_MODEL_GATEWAY_REQUEST_TIMEOUT_S", "600"))
            max_inflight = int(env.get("LAB_MODEL_GATEWAY_MAX_INFLIGHT_PER_RUN", "2"))
            reasoning_ttl = int(env.get("LAB_MODEL_GATEWAY_REASONING_TTL_S", "900"))
            reasoning_entries = int(env.get("LAB_MODEL_GATEWAY_REASONING_MAX_ENTRIES", "4096"))
            renewal_ttl = int(env.get("LAB_MODEL_GATEWAY_TOKEN_RENEWAL_TTL_S", "300"))
        except ValueError as exc:
            raise ValueError("model gateway numeric configuration is invalid") from exc
        if (
            not 1 <= port <= 65535
            or min(max_bytes, max_output, max_inflight, reasoning_ttl, reasoning_entries) <= 0
            or not 60 <= renewal_ttl <= 900
            or timeout <= 0
        ):
            raise ValueError("model gateway numeric configuration is out of range")
        return cls(
            bind_host=env.get("LAB_MODEL_GATEWAY_BIND_HOST", "0.0.0.0"),
            bind_port=port,
            upstream_base_url=_upstream_base_url(
                _required(env, "LAB_MODEL_GATEWAY_UPSTREAM_BASE_URL", "LLM_BASE_URL")
            ),
            upstream_api_key=_required(
                env, "LAB_MODEL_GATEWAY_UPSTREAM_API_KEY", "LLM_API_KEY"
            ),
            auth_secret=secret,
            ledger_path=env.get(
                "LAB_MODEL_GATEWAY_LEDGER_PATH", "/var/lib/simverse/model-gateway.db"
            ),
            request_timeout_s=timeout,
            max_request_bytes=max_bytes,
            default_max_output_tokens=max_output,
            # Pricing is CNY per million tokens. Defaults are the published
            # Global deployment prices as of 2026-07-27; operators can override.
            cny_per_usd=_decimal(env, "LAB_MODEL_GATEWAY_CNY_PER_USD", "7.0"),
            flash_input_cny_per_million=_decimal(
                env, "LAB_MODEL_GATEWAY_FLASH_INPUT_CNY_PER_MILLION", "1"
            ),
            flash_output_cny_per_million=_decimal(
                env, "LAB_MODEL_GATEWAY_FLASH_OUTPUT_CNY_PER_MILLION", "2"
            ),
            pro_input_cny_per_million=_decimal(
                env, "LAB_MODEL_GATEWAY_PRO_INPUT_CNY_PER_MILLION", "12"
            ),
            pro_output_cny_per_million=_decimal(
                env, "LAB_MODEL_GATEWAY_PRO_OUTPUT_CNY_PER_MILLION", "24"
            ),
            max_inflight_per_run=max_inflight,
            reasoning_ttl_s=reasoning_ttl,
            reasoning_max_entries=reasoning_entries,
            token_renewal_ttl_s=renewal_ttl,
        )

    def prices_for(self, model: str) -> tuple[Decimal, Decimal]:
        if model == FLASH_MODEL:
            return self.flash_input_cny_per_million, self.flash_output_cny_per_million
        if model == PRO_MODEL:
            return self.pro_input_cny_per_million, self.pro_output_cny_per_million
        raise ValueError("model is not supported")
