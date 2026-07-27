from __future__ import annotations

from dataclasses import dataclass

import jwt

from app.lab.model_catalog import (
    ALLOWED_MODELS,
    FLASH_MODEL,
    MODEL_GATEWAY_AUDIENCE,
    MODEL_GATEWAY_ISSUER,
    PRO_MODEL,
    RESOURCE_PROFILES,
)


class GatewayAuthError(ValueError):
    pass


@dataclass(frozen=True)
class GatewayClaims:
    tenant_id: str
    task_id: str
    run_id: str
    model_tier: str
    model: str
    model_policy_version: str
    budget_usd_cents: int
    max_model_tokens: int
    resource_cpu_cores: int
    resource_memory_mb: int
    jti: str


def verify_gateway_token(token: str, secret: str) -> GatewayClaims:
    try:
        raw = jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience=MODEL_GATEWAY_AUDIENCE,
            issuer=MODEL_GATEWAY_ISSUER,
            options={"require": ["exp", "nbf", "iat", "jti", "sub"]},
        )
    except jwt.PyJWTError as exc:
        raise GatewayAuthError("invalid model gateway token") from exc
    required_strings = (
        "tenant_id",
        "task_id",
        "run_id",
        "model_tier",
        "model",
        "model_policy_version",
        "jti",
    )
    if any(not isinstance(raw.get(key), str) or not raw[key] for key in required_strings):
        raise GatewayAuthError("invalid model gateway claims")
    if raw.get("sub") != raw["run_id"]:
        raise GatewayAuthError("model gateway subject mismatch")
    expected = {"low": FLASH_MODEL, "high": PRO_MODEL}
    if raw["model"] not in ALLOWED_MODELS or expected.get(raw["model_tier"]) != raw["model"]:
        raise GatewayAuthError("model tier is not authorized")
    budget = raw.get("budget_usd_cents")
    max_tokens = raw.get("max_model_tokens")
    cpu_cores = raw.get("resource_cpu_cores")
    memory_mb = raw.get("resource_memory_mb")
    if type(budget) is not int or budget <= 0 or type(max_tokens) is not int or max_tokens <= 0:
        raise GatewayAuthError("model gateway budget claims are invalid")
    profile = RESOURCE_PROFILES[raw["model_tier"]]
    if (
        cpu_cores != profile["cpu_cores"]
        or memory_mb != profile["memory_mb"]
    ):
        raise GatewayAuthError("model gateway resource profile is invalid")
    return GatewayClaims(
        tenant_id=raw["tenant_id"],
        task_id=raw["task_id"],
        run_id=raw["run_id"],
        model_tier=raw["model_tier"],
        model=raw["model"],
        model_policy_version=raw["model_policy_version"],
        budget_usd_cents=budget,
        max_model_tokens=max_tokens,
        resource_cpu_cores=cpu_cores,
        resource_memory_mb=memory_mb,
        jti=raw["jti"],
    )
