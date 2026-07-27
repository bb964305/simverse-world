"""Reward-bound model routing for Codex-backed Lab runs."""
from __future__ import annotations

import math
import time
import uuid
from dataclasses import dataclass

import jwt

from app.config import settings
from app.lab.model_catalog import (
    ALLOWED_MODELS,
    FLASH_MODEL,
    MODEL_GATEWAY_AUDIENCE,
    MODEL_GATEWAY_ISSUER,
    PRO_MODEL,
    RESOURCE_PROFILES,
)


class ModelPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class ModelAssignment:
    tier: str
    model: str
    policy_version: str
    budget_usd_cents: int
    cpu_cores: int
    memory_mb: int


def assignment_for_reward(reward_sc: int) -> ModelAssignment:
    if type(reward_sc) is not int or reward_sc < 0:
        raise ModelPolicyError("reward_sc must be a non-negative integer")
    threshold = settings.lab_pro_min_reward_sc
    if type(threshold) is not int or threshold <= 0:
        raise ModelPolicyError("lab_pro_min_reward_sc must be positive")
    if reward_sc >= threshold:
        tier = "high"
        model = PRO_MODEL
        budget_usd = settings.lab_pro_budget_usd
    else:
        tier = "low"
        model = FLASH_MODEL
        budget_usd = settings.lab_flash_budget_usd
    if budget_usd <= 0:
        raise ModelPolicyError(f"{tier} model budget must be positive")
    return ModelAssignment(
        tier=tier,
        model=model,
        policy_version=settings.lab_model_policy_version,
        budget_usd_cents=int(math.ceil(budget_usd * 100)),
        cpu_cores=RESOURCE_PROFILES[tier]["cpu_cores"],
        memory_mb=RESOURCE_PROFILES[tier]["memory_mb"],
    )


def issue_gateway_token(
    *,
    tenant_id: str,
    task_id: str,
    run_id: str,
    assignment: ModelAssignment,
    max_model_tokens: int,
    now: int | None = None,
) -> str:
    secret = settings.lab_model_gateway_auth_secret
    if not isinstance(secret, str) or len(secret.encode("utf-8")) < 32:
        raise ModelPolicyError(
            "lab_model_gateway_auth_secret must contain at least 32 bytes"
        )
    if assignment.model not in ALLOWED_MODELS:
        raise ModelPolicyError("model is not allowed by the Lab policy")
    if type(max_model_tokens) is not int or max_model_tokens <= 0:
        raise ModelPolicyError("max_model_tokens must be positive")
    ttl = settings.lab_model_gateway_token_ttl_s
    if type(ttl) is not int or not 60 <= ttl <= 86_400:
        raise ModelPolicyError(
            "lab_model_gateway_token_ttl_s must be between 60 and 86400"
        )
    issued_at = int(time.time()) if now is None else now
    return jwt.encode(
        {
            "iss": MODEL_GATEWAY_ISSUER,
            "aud": MODEL_GATEWAY_AUDIENCE,
            "sub": run_id,
            "jti": str(uuid.uuid4()),
            "tenant_id": tenant_id,
            "task_id": task_id,
            "run_id": run_id,
            "model_tier": assignment.tier,
            "model": assignment.model,
            "model_policy_version": assignment.policy_version,
            "budget_usd_cents": assignment.budget_usd_cents,
            "resource_cpu_cores": assignment.cpu_cores,
            "resource_memory_mb": assignment.memory_mb,
            "max_model_tokens": max_model_tokens,
            "iat": issued_at,
            "nbf": issued_at,
            "exp": issued_at + ttl,
        },
        secret,
        algorithm="HS256",
    )
