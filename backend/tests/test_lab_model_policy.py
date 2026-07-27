import time

import jwt
import pytest

from app.config import settings
from app.lab.model_policy import (
    FLASH_MODEL,
    PRO_MODEL,
    ModelPolicyError,
    assignment_for_reward,
    issue_gateway_token,
)


def test_reward_selects_only_flash_or_pro(monkeypatch):
    monkeypatch.setattr(settings, "lab_pro_min_reward_sc", 100)
    monkeypatch.setattr(settings, "lab_flash_budget_usd", 0.25)
    monkeypatch.setattr(settings, "lab_pro_budget_usd", 0.5)
    monkeypatch.setattr(settings, "lab_model_policy_version", "policy-test")

    low = assignment_for_reward(99)
    high = assignment_for_reward(100)

    assert (low.tier, low.model, low.budget_usd_cents) == ("low", FLASH_MODEL, 25)
    assert (low.cpu_cores, low.memory_mb) == (2, 2048)
    assert (high.tier, high.model, high.budget_usd_cents) == ("high", PRO_MODEL, 50)
    assert (high.cpu_cores, high.memory_mb) == (4, 4096)
    assert low.policy_version == high.policy_version == "policy-test"


def test_gateway_token_binds_run_model_and_budget(monkeypatch):
    secret = "gateway-test-secret-that-is-at-least-32-bytes"
    monkeypatch.setattr(settings, "lab_model_gateway_auth_secret", secret)
    monkeypatch.setattr(settings, "lab_model_gateway_token_ttl_s", 600)
    monkeypatch.setattr(settings, "lab_pro_min_reward_sc", 100)
    monkeypatch.setattr(settings, "lab_flash_budget_usd", 0.25)
    assignment = assignment_for_reward(10)

    now = int(time.time())
    token = issue_gateway_token(
        tenant_id="tenant", task_id="task", run_id="run",
        assignment=assignment, max_model_tokens=1234, now=now,
    )
    raw = jwt.decode(
        token, secret, algorithms=["HS256"], options={"verify_signature": True,
        "verify_exp": False, "verify_aud": False},
    )
    assert raw["model"] == FLASH_MODEL
    assert raw["budget_usd_cents"] == 25
    assert raw["max_model_tokens"] == 1234
    assert raw["resource_cpu_cores"] == 2
    assert raw["resource_memory_mb"] == 2048


def test_gateway_token_rejects_short_secret(monkeypatch):
    monkeypatch.setattr(settings, "lab_model_gateway_auth_secret", "short")
    monkeypatch.setattr(settings, "lab_pro_min_reward_sc", 100)
    monkeypatch.setattr(settings, "lab_flash_budget_usd", 0.25)
    with pytest.raises(ModelPolicyError, match="32 bytes"):
        issue_gateway_token(
            tenant_id="tenant", task_id="task", run_id="run",
            assignment=assignment_for_reward(10), max_model_tokens=100,
        )
