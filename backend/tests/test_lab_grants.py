"""T2 — signed run-scoped capability grants: sign/verify round-trip, expiry,
issuance + attenuated delegation, and revocation (PRD §Run-scoped Grant,
§Capability and Approval Model, V01-V03/V14).
"""
from datetime import datetime, UTC

import pytest

from app.lab import grants
from app.lab import policy
from app.lab.protocol import GrantClaims
from app.models.lab_grant import LabCapabilityGrant


@pytest.fixture(autouse=True)
def _grant_secret(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "lab_grant_secret", "test-secret", raising=False)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _claims(**overrides):
    now = int(datetime.now(UTC).timestamp())
    kwargs = dict(
        iss="lab-runtime", aud="tool-broker", jti="jti-1", tenant_id="t1",
        task_id="task1", run_id="run1", agent_id="agent-1", depth=0,
        capabilities=["web_search"], budgets={"model_tokens": 1000},
        policy_version="lab-policy-v1", fencing_epoch=0, nbf=now, exp=now + 900,
    )
    kwargs.update(overrides)
    return GrantClaims(**kwargs)


# ─── 1-2. sign / verify round-trip + tamper detection ──────────────


def test_sign_verify_round_trip():
    claims = _claims()
    token = grants.sign_grant(claims)
    assert grants.verify_grant(token) == claims


def test_verify_rejects_tampered_payload_byte():
    token = grants.sign_grant(_claims())
    payload_b64, sig = token.split(".", 1)
    tampered_char = "A" if payload_b64[0] != "A" else "B"
    tampered_token = f"{tampered_char}{payload_b64[1:]}.{sig}"
    with pytest.raises(grants.GrantError):
        grants.verify_grant(tampered_token)


def test_verify_rejects_tampered_signature():
    token = grants.sign_grant(_claims())
    payload_b64, sig = token.split(".", 1)
    tampered_char = "0" if sig[0] != "0" else "1"
    tampered_token = f"{payload_b64}.{tampered_char}{sig[1:]}"
    with pytest.raises(grants.GrantError):
        grants.verify_grant(tampered_token)


def test_verify_rejects_expired_grant():
    now = int(datetime.now(UTC).timestamp())
    token = grants.sign_grant(_claims(nbf=now - 1000, exp=now - 100))
    with pytest.raises(grants.GrantError):
        grants.verify_grant(token, now=now)


def test_verify_rejects_not_yet_valid_grant():
    now = int(datetime.now(UTC).timestamp())
    token = grants.sign_grant(_claims(nbf=now + 100, exp=now + 900))
    with pytest.raises(grants.GrantError):
        grants.verify_grant(token, now=now)


# ─── 3. issue_run_grant persistence + defaults ─────────────────────


@pytest.mark.anyio
async def test_issue_run_grant_persists_row_with_matching_hash(db_session):
    token, claims = await grants.issue_run_grant(
        db_session, tenant_id="t1", task_id="task1", run_id="run1",
        agent_id="agent-1", capabilities=["web_search"],
    )
    assert claims.depth == 0
    assert claims.parent_jti is None
    assert claims.exp - claims.nbf == 900

    row = await db_session.get(LabCapabilityGrant, claims.jti)
    assert row is not None
    assert row.grant_hash == grants.grant_hash(token)
    assert row.revoked_at is None


@pytest.mark.anyio
async def test_issue_run_grant_default_budgets_from_settings(db_session):
    from app.config import settings
    _, claims = await grants.issue_run_grant(
        db_session, tenant_id="t1", task_id="task1", run_id="run1",
        agent_id="agent-1", capabilities=["web_search"],
    )
    assert claims.budgets == {
        "model_tokens": settings.lab_budget_model_tokens,
        "tool_calls": settings.lab_budget_tool_calls,
        "wall_clock_ms": settings.lab_budget_wall_clock_ms,
        "egress_requests": settings.lab_budget_egress_requests,
        "egress_bytes": settings.lab_budget_egress_bytes,
        "artifact_count": settings.lab_budget_artifact_count,
        "artifact_bytes": settings.lab_budget_artifact_bytes,
        "active_workers": settings.lab_budget_active_workers,
    }


# ─── 4. child-grant attenuation ─────────────────────────────────────


@pytest.mark.anyio
async def test_child_grant_capabilities_must_be_subset(db_session):
    _, parent = await grants.issue_run_grant(
        db_session, tenant_id="t1", task_id="task1", run_id="run1",
        agent_id="agent-1", capabilities=["web_search"],
    )
    with pytest.raises(grants.GrantError):
        await grants.issue_run_grant(
            db_session, tenant_id="t1", task_id="task1", run_id="run1",
            agent_id="agent-2", capabilities=["web_search", "http"], parent=parent,
        )


@pytest.mark.anyio
async def test_child_grant_budgets_must_not_exceed_parent(db_session):
    _, parent = await grants.issue_run_grant(
        db_session, tenant_id="t1", task_id="task1", run_id="run1",
        agent_id="agent-1", capabilities=["web_search"], budgets={"model_tokens": 100},
    )
    with pytest.raises(grants.GrantError):
        await grants.issue_run_grant(
            db_session, tenant_id="t1", task_id="task1", run_id="run1",
            agent_id="agent-2", capabilities=["web_search"],
            budgets={"model_tokens": 200}, parent=parent,
        )


@pytest.mark.anyio
async def test_child_grant_egress_must_be_subset(db_session):
    _, parent = await grants.issue_run_grant(
        db_session, tenant_id="t1", task_id="task1", run_id="run1",
        agent_id="agent-1", capabilities=["http"], egress=["example.com"],
    )
    with pytest.raises(grants.GrantError):
        await grants.issue_run_grant(
            db_session, tenant_id="t1", task_id="task1", run_id="run1",
            agent_id="agent-2", capabilities=["http"],
            egress=["example.com", "evil.com"], parent=parent,
        )


@pytest.mark.anyio
async def test_grandchild_grant_depth_exceeded(db_session):
    _, parent = await grants.issue_run_grant(
        db_session, tenant_id="t1", task_id="task1", run_id="run1",
        agent_id="agent-1", capabilities=["web_search"],
    )
    _, child = await grants.issue_run_grant(
        db_session, tenant_id="t1", task_id="task1", run_id="run1",
        agent_id="agent-2", capabilities=["web_search"], parent=parent,
    )
    with pytest.raises(grants.GrantError):
        await grants.issue_run_grant(
            db_session, tenant_id="t1", task_id="task1", run_id="run1",
            agent_id="agent-3", capabilities=["web_search"], parent=child,
        )


@pytest.mark.anyio
async def test_child_grant_exp_not_later_than_parent(db_session):
    _, parent = await grants.issue_run_grant(
        db_session, tenant_id="t1", task_id="task1", run_id="run1",
        agent_id="agent-1", capabilities=["web_search"], ttl_s=100,
    )
    _, child = await grants.issue_run_grant(
        db_session, tenant_id="t1", task_id="task1", run_id="run1",
        agent_id="agent-2", capabilities=["web_search"], parent=parent, ttl_s=900,
    )
    assert child.exp == parent.exp


@pytest.mark.anyio
async def test_child_grant_valid_subset_succeeds_with_parent_jti(db_session):
    _, parent = await grants.issue_run_grant(
        db_session, tenant_id="t1", task_id="task1", run_id="run1",
        agent_id="agent-1", capabilities=["web_search", "http"],
        egress=["example.com"], budgets={"model_tokens": 1000},
    )
    _, child = await grants.issue_run_grant(
        db_session, tenant_id="t1", task_id="task1", run_id="run1",
        agent_id="agent-2", capabilities=["web_search"], egress=["example.com"],
        budgets={"model_tokens": 500}, parent=parent,
    )
    assert child.depth == 1
    assert child.parent_jti == parent.jti
    row = await db_session.get(LabCapabilityGrant, child.jti)
    assert row.parent_jti == parent.jti
    assert row.depth == 1


@pytest.mark.anyio
async def test_child_grant_tenant_task_run_must_match_parent(db_session):
    _, parent = await grants.issue_run_grant(
        db_session, tenant_id="t1", task_id="task1", run_id="run1",
        agent_id="agent-1", capabilities=["web_search"],
    )
    with pytest.raises(grants.GrantError):
        await grants.issue_run_grant(
            db_session, tenant_id="t2", task_id="task1", run_id="run1",
            agent_id="agent-2", capabilities=["web_search"], parent=parent,
        )


# ─── 5-6. revocation + fencing epoch ────────────────────────────────


@pytest.mark.anyio
async def test_revoke_grant_then_check_active_fails(db_session):
    _, claims = await grants.issue_run_grant(
        db_session, tenant_id="t1", task_id="task1", run_id="run1",
        agent_id="agent-1", capabilities=["web_search"],
    )
    await grants.check_grant_active(db_session, claims)  # active: no raise

    await grants.revoke_grant(db_session, claims.jti)
    with pytest.raises(grants.GrantError):
        await grants.check_grant_active(db_session, claims)


@pytest.mark.anyio
async def test_revoke_run_grants_revokes_all_for_run(db_session):
    _, c1 = await grants.issue_run_grant(
        db_session, tenant_id="t1", task_id="task1", run_id="run1",
        agent_id="agent-1", capabilities=["web_search"],
    )
    _, c2 = await grants.issue_run_grant(
        db_session, tenant_id="t1", task_id="task1", run_id="run1",
        agent_id="agent-2", capabilities=["web_search"], parent=c1,
    )

    await grants.revoke_run_grants(db_session, "run1")

    with pytest.raises(grants.GrantError):
        await grants.check_grant_active(db_session, c1)
    with pytest.raises(grants.GrantError):
        await grants.check_grant_active(db_session, c2)


@pytest.mark.anyio
async def test_check_grant_active_rejects_stale_epoch(db_session):
    _, claims = await grants.issue_run_grant(
        db_session, tenant_id="t1", task_id="task1", run_id="run1",
        agent_id="agent-1", capabilities=["web_search"], fencing_epoch=0,
    )
    with pytest.raises(grants.GrantError, match="stale"):
        await grants.check_grant_active(db_session, claims, expected_epoch=1)


# ─── 7. V14 non-elevation: decide() ignores reasoning/deliberation mode ─────


@pytest.mark.anyio
async def test_v14_policy_decisions_identical_regardless_of_deliberate_budgets(db_session):
    """A 'deliberate' scenario is simulated purely by inflating
    budgets['model_tokens'] — nothing about reasoning mode reaches decide().
    Every registered tool must get the exact same effect/risk_class/hard_deny
    under both budget regimes."""
    all_caps = list({t.capability for t in policy.TOOL_REGISTRY.values()})

    _, normal = await grants.issue_run_grant(
        db_session, tenant_id="t1", task_id="task1", run_id="run1",
        agent_id="agent-1", capabilities=all_caps, egress=["example.com"],
        budgets={"model_tokens": 1000},
    )
    _, deliberate = await grants.issue_run_grant(
        db_session, tenant_id="t1", task_id="task2", run_id="run2",
        agent_id="agent-1", capabilities=all_caps, egress=["example.com"],
        budgets={"model_tokens": 1_000_000},
    )

    for tool_name in policy.TOOL_REGISTRY:
        d_normal = policy.decide(tool_name, {}, normal)
        d_deliberate = policy.decide(tool_name, {}, deliberate)
        assert d_normal.effect == d_deliberate.effect
        assert d_normal.risk_class == d_deliberate.risk_class
        assert d_normal.hard_deny == d_deliberate.hard_deny
