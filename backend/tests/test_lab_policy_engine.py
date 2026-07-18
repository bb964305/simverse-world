"""T2 — Policy Engine: deny > ask > allow, R0-R4 risk classes, fail-closed on
unknown tools (PRD §Capability and Approval Model, V01/V02). Pure decision
function — no DB, no I/O.
"""
from datetime import datetime, UTC

import pytest

from app.lab import policy
from app.lab.protocol import GrantClaims


def _claims(capabilities, **overrides):
    now = int(datetime.now(UTC).timestamp())
    kwargs = dict(
        iss="lab-runtime", aud="tool-broker", jti="jti-1", tenant_id="t1",
        task_id="task1", run_id="run1", agent_id="agent-1", depth=0,
        capabilities=capabilities, budgets={"model_tokens": 1000},
        policy_version="lab-policy-v1", fencing_epoch=0, nbf=now, exp=now + 900,
    )
    kwargs.update(overrides)
    return GrantClaims(**kwargs)


# ─── 1. unknown tool ─────────────────────────────────────────────


def test_unknown_tool_is_hard_denied():
    claims = _claims(["web_search", "http", "code", "financial", "world_apply"])
    d = policy.decide("nonexistent.tool", {}, claims)
    assert d.effect == "deny"
    assert d.risk_class == "R4"
    assert d.reason == "unknown_tool"
    assert d.hard_deny is True
    assert d.requires_approval is False


# ─── 2. R4 tools deny regardless of granted capabilities ─────────


@pytest.mark.parametrize("tool_name", ["payment.charge", "wallet.transfer", "world.apply"])
def test_r4_tools_always_hard_denied_even_with_capability_granted(tool_name):
    tool = policy.TOOL_REGISTRY[tool_name]
    claims = _claims([tool.capability])
    d = policy.decide(tool_name, {}, claims)
    assert d.effect == "deny"
    assert d.hard_deny is True
    assert d.reason == "hard_deny"
    assert d.requires_approval is False


# ─── 3. web.search: allow with capability, deny without ─────────


def test_web_search_allowed_with_capability():
    claims = _claims(["web_search"])
    d = policy.decide("web.search", {}, claims)
    assert d.effect == "allow"
    assert d.risk_class == "R0"
    assert d.hard_deny is False
    assert d.requires_approval is False


def test_web_search_denied_without_capability():
    claims = _claims([])
    d = policy.decide("web.search", {}, claims)
    assert d.effect == "deny"
    assert d.reason == "capability_not_granted"
    assert d.requires_approval is False
    assert d.hard_deny is False


# ─── 4. http.request: ask ─────────────────────────────────────────


def test_http_request_requires_approval():
    claims = _claims(["http"])
    d = policy.decide("http.request", {}, claims)
    assert d.effect == "ask"
    assert d.requires_approval is True
    assert d.hard_deny is False


# ─── 5. world.propose: govern (neither ask nor allow) ────────────


def test_world_propose_is_governed_not_ask_or_allow():
    claims = _claims(["world_propose"])
    d = policy.decide("world.propose", {}, claims)
    assert d.effect == "govern"
    assert d.effect not in ("ask", "allow")
    assert d.requires_approval is False
    assert d.hard_deny is False


# ─── 6. unregistered financial-looking names ──────────────────────


@pytest.mark.parametrize("tool_name", ["checkout.session", "wire.send"])
def test_unregistered_financial_looking_names_are_hard_denied(tool_name):
    claims = _claims(["financial"])
    d = policy.decide(tool_name, {}, claims)
    assert d.effect == "deny"
    assert d.hard_deny is True
    assert d.reason == "unregistered_financial"


# ─── 7. deny > ask > allow: no deny ever approvable ───────────────


@pytest.mark.parametrize("tool_name,capabilities", [
    ("nonexistent.tool", []),
    ("payment.charge", ["financial"]),
    ("wallet.transfer", ["financial"]),
    ("world.apply", ["world_apply"]),
    ("web.search", []),
    ("checkout.session", ["financial"]),
])
def test_deny_decisions_never_require_approval(tool_name, capabilities):
    claims = _claims(capabilities)
    d = policy.decide(tool_name, {}, claims)
    assert d.effect == "deny"
    assert d.requires_approval is False


# ─── decide() signature is mode-independent (V14) ─────────────────


def test_decide_signature_has_no_reasoning_mode_parameter():
    import inspect
    params = list(inspect.signature(policy.decide).parameters)
    assert params == ["tool_name", "args", "claims"]
    assert "mode" not in params
    assert "reasoning_mode" not in params
    assert "deliberate" not in params
