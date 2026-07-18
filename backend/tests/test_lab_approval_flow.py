"""T3 — Tool Broker + canonical approvals (PRD §Tool Contract, §Capability and
Approval Model authoritative sequence 1-7 / V01, V02, V09).

The Broker is THE enforcement point: every effect crosses it. These tests pin
the load-bearing invariants — a hard deny never creates an approval row, a
forged approval cannot execute an R4 tool (policy is re-evaluated immediately
before execution), an approved args-digest binds the exact args, one-shot
atomic consumption makes double-execution impossible, egress targets are
screened, and every denial leaves an audit row.
"""
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, func

from app.lab import broker, grants
from app.models.lab_action import LabToolAction, LabApproval


@pytest.fixture(autouse=True)
def _grant_secret(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "lab_grant_secret", "test-secret", raising=False)


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _grant(db, caps, *, egress=None, fencing_epoch=0, budgets=None):
    """Issue + persist a real signed grant so check_grant_active finds a row."""
    token, claims = await grants.issue_run_grant(
        db, tenant_id="owner-1", task_id="task1", run_id="run1",
        agent_id="agent-1", capabilities=list(caps),
        egress=egress or [], budgets=budgets, fencing_epoch=fencing_epoch,
    )
    return token, claims


async def _count(db, model):
    res = await db.execute(select(func.count()).select_from(model))
    return res.scalar_one()


async def _approval_for(db, action_id):
    res = await db.execute(select(LabApproval).where(LabApproval.action_id == action_id))
    return res.scalar_one()


# ─── 1. V01a: unknown tool → denied audit row, zero approvals ─────────


@pytest.mark.anyio
async def test_v01a_unknown_tool_denied_with_audit_and_no_approval(db_session):
    token, claims = await _grant(db_session, ["web_search"])
    executor = AsyncMock()

    with pytest.raises(broker.ActionDenied):
        await broker.request_action(
            db_session, claims=claims, token=token,
            tool_name="nonexistent.tool", args={"x": 1},
        )

    assert await _count(db_session, LabToolAction) == 1
    action = (await db_session.execute(select(LabToolAction))).scalar_one()
    assert action.status == "denied"
    assert await _count(db_session, LabApproval) == 0
    executor.assert_not_called()


# ─── 2. V01b: http.request (R2) → waiting_approval + one pending row ───


@pytest.mark.anyio
async def test_v01b_r2_tool_waits_for_approval(db_session):
    token, claims = await _grant(db_session, ["http"], egress=["*.example.org"])
    executor = AsyncMock()

    action = await broker.request_action(
        db_session, claims=claims, token=token, tool_name="http.request",
        args={"url": "https://api.example.org/data", "method": "POST"},
    )

    assert action.status == "waiting_approval"
    approvals = (await db_session.execute(select(LabApproval))).scalars().all()
    assert len(approvals) == 1
    assert approvals[0].decision == "pending"
    assert approvals[0].action_id == action.id
    executor.assert_not_called()


# ─── 3. V02: R4 hard deny + forged approval cannot execute ────────────


@pytest.mark.anyio
@pytest.mark.parametrize("tool_name,cap", [
    ("payment.charge", "financial"),
    ("world.apply", "world_apply"),
])
async def test_v02_r4_denied_and_forged_approval_cannot_execute(db_session, tool_name, cap):
    token, claims = await _grant(db_session, [cap])
    executor = AsyncMock()
    args = {"amount": 100}

    with pytest.raises(broker.ActionDenied) as ei:
        await broker.request_action(
            db_session, claims=claims, token=token, tool_name=tool_name, args=args,
        )
    action = ei.value.action
    assert action.status == "denied"
    assert await _count(db_session, LabApproval) == 0

    # Forge an "approved" approval and flip the action to approved — the Broker
    # must still refuse because it re-evaluates policy right before execution.
    db_session.add(LabApproval(
        id="forged-1", tenant_id=claims.tenant_id, run_id=claims.run_id,
        task_id=claims.task_id, action_id=action.id, args_digest=action.args_hash,
        decision="approved", expires_at=datetime.now(UTC) + timedelta(hours=1),
    ))
    action.status = "approved"
    action.approval_id = "forged-1"
    await db_session.commit()

    with pytest.raises(broker.ActionDenied):
        await broker.execute_action(
            db_session, action_id=action.id, claims=claims, executor=executor, args=args,
        )
    executor.assert_not_called()
    refreshed = await db_session.get(LabToolAction, action.id)
    assert refreshed.status == "denied"


# ─── 4. V09a: approved args-digest binds exact args ───────────────────


@pytest.mark.anyio
async def test_v09a_digest_mismatch_invalidates_approval(db_session):
    token, claims = await _grant(db_session, ["http"], egress=["*.example.org"])
    executor = AsyncMock()
    args = {"url": "https://api.example.org/data", "method": "POST", "body": "a"}

    action = await broker.request_action(
        db_session, claims=claims, token=token, tool_name="http.request", args=args,
    )
    appr = await _approval_for(db_session, action.id)
    await broker.decide_approval(
        db_session, approval_id=appr.id, decider_user_id="owner-1",
        approve=True, task_owner_id="owner-1",
    )

    changed = dict(args, body="b")  # same host (egress ok), different digest
    with pytest.raises(broker.ApprovalInvalid) as ei:
        await broker.execute_action(
            db_session, action_id=action.id, claims=claims, executor=executor, args=changed,
        )
    assert ei.value.reason == "digest_mismatch"
    executor.assert_not_called()


# ─── 5. V09b: actor binding (owner / admin can decide, others cannot) ─


@pytest.mark.anyio
async def test_v09b_actor_binding(db_session):
    token, claims = await _grant(db_session, ["http"], egress=["*.example.org"])
    args = {"url": "https://api.example.org/x"}

    a1 = await broker.request_action(
        db_session, claims=claims, token=token, tool_name="http.request", args=args,
    )
    appr1 = await _approval_for(db_session, a1.id)

    with pytest.raises(broker.ApprovalInvalid) as ei:
        await broker.decide_approval(
            db_session, approval_id=appr1.id, decider_user_id="intruder",
            approve=True, task_owner_id="owner-1",
        )
    assert ei.value.reason == "actor"

    res = await broker.decide_approval(
        db_session, approval_id=appr1.id, decider_user_id="owner-1",
        approve=True, task_owner_id="owner-1",
    )
    assert res.decision == "approved"
    assert (await db_session.get(LabToolAction, a1.id)).status == "approved"

    a2 = await broker.request_action(
        db_session, claims=claims, token=token, tool_name="http.request",
        args=dict(args, n=2),
    )
    appr2 = await _approval_for(db_session, a2.id)
    res2 = await broker.decide_approval(
        db_session, approval_id=appr2.id, decider_user_id="admin-x",
        approve=True, task_owner_id="owner-1", is_admin=True,
    )
    assert res2.decision == "approved"


# ─── 6. V09c: expiry rejects both decide and execute ──────────────────


@pytest.mark.anyio
async def test_v09c_expiry_rejects_decide_and_execute(db_session):
    token, claims = await _grant(db_session, ["http"], egress=["*.example.org"])
    args = {"url": "https://api.example.org/x"}

    # (a) pending approval whose window has passed → decide rejects as expired
    a1 = await broker.request_action(
        db_session, claims=claims, token=token, tool_name="http.request", args=args,
    )
    appr1 = await _approval_for(db_session, a1.id)
    appr1.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.commit()
    with pytest.raises(broker.ApprovalInvalid) as ei:
        await broker.decide_approval(
            db_session, approval_id=appr1.id, decider_user_id="owner-1",
            approve=True, task_owner_id="owner-1",
        )
    assert ei.value.reason == "expired"

    # (b) approved but window passed before execution → execute refuses to consume
    a2 = await broker.request_action(
        db_session, claims=claims, token=token, tool_name="http.request",
        args=dict(args, n=2),
    )
    appr2 = await _approval_for(db_session, a2.id)
    await broker.decide_approval(
        db_session, approval_id=appr2.id, decider_user_id="owner-1",
        approve=True, task_owner_id="owner-1",
    )
    appr2.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.commit()
    executor = AsyncMock()
    with pytest.raises(broker.ApprovalInvalid):
        await broker.execute_action(
            db_session, action_id=a2.id, claims=claims, executor=executor, args=dict(args, n=2),
        )
    executor.assert_not_called()


# ─── 7. V09d: one-shot atomic consume — no double execution ───────────


@pytest.mark.anyio
async def test_v09d_one_shot_consume_no_double_execution(db_session):
    token, claims = await _grant(db_session, ["http"], egress=["*.example.org"])
    args = {"url": "https://api.example.org/x"}

    a = await broker.request_action(
        db_session, claims=claims, token=token, tool_name="http.request", args=args,
    )
    appr = await _approval_for(db_session, a.id)
    await broker.decide_approval(
        db_session, approval_id=appr.id, decider_user_id="owner-1",
        approve=True, task_owner_id="owner-1",
    )

    executor = AsyncMock(return_value={"ok": True})
    r1 = await broker.execute_action(
        db_session, action_id=a.id, claims=claims, executor=executor, args=args,
    )
    assert r1.status == "succeeded"
    assert executor.await_count == 1

    # Re-execute a succeeded action → idempotent, executor not called again.
    r2 = await broker.execute_action(
        db_session, action_id=a.id, claims=claims, executor=executor, args=args,
    )
    assert r2.status == "succeeded"
    assert executor.await_count == 1

    # Force the action back to "approved" — the already-consumed approval must
    # make the conditional UPDATE match zero rows (not_consumable).
    a.status = "approved"
    await db_session.commit()
    with pytest.raises(broker.ApprovalInvalid) as ei:
        await broker.execute_action(
            db_session, action_id=a.id, claims=claims, executor=executor, args=args,
        )
    assert ei.value.reason == "not_consumable"
    assert executor.await_count == 1


# ─── 8. allow passthrough executes and redacts stored result ──────────


@pytest.mark.anyio
async def test_allow_passthrough_executes_and_redacts_result(db_session):
    token, claims = await _grant(db_session, ["web_search"])
    action = await broker.request_action(
        db_session, claims=claims, token=token, tool_name="web.search",
        args={"query": "hello"},
    )
    assert action.status == "approved"
    assert await _count(db_session, LabApproval) == 0

    executor = AsyncMock(return_value={"token": "sk-abcdef0123456789", "data": "result"})
    res = await broker.execute_action(
        db_session, action_id=action.id, claims=claims, executor=executor,
        args={"query": "hello"},
    )
    assert res.status == "succeeded"
    assert executor.await_count == 1
    assert res.result_json["token"] == "[REDACTED]"


# ─── 9. idempotency key returns the same action, no duplicate row ─────


@pytest.mark.anyio
async def test_idempotent_request_returns_same_action(db_session):
    token, claims = await _grant(db_session, ["web_search"])
    a1 = await broker.request_action(
        db_session, claims=claims, token=token, tool_name="web.search",
        args={"query": "x"}, idempotency_key="idem-1",
    )
    a2 = await broker.request_action(
        db_session, claims=claims, token=token, tool_name="web.search",
        args={"query": "x"}, idempotency_key="idem-1",
    )
    assert a1.id == a2.id
    assert await _count(db_session, LabToolAction) == 1


# ─── 10. stale fencing epoch → denied audit row ───────────────────────


@pytest.mark.anyio
async def test_stale_epoch_denied_with_audit(db_session):
    token, claims = await _grant(db_session, ["web_search"], fencing_epoch=0)
    with pytest.raises(broker.ActionDenied) as ei:
        await broker.request_action(
            db_session, claims=claims, token=token, tool_name="web.search",
            args={"query": "x"}, expected_epoch=1,
        )
    action = ei.value.action
    assert action.status == "denied"
    assert "stale" in action.result_json["reason"]
    assert await _count(db_session, LabToolAction) == 1
    assert await _count(db_session, LabApproval) == 0


# ─── 11. UncertainOutcome → reconciliation_required, no auto-retry ────


@pytest.mark.anyio
async def test_uncertain_outcome_reconciliation_no_retry(db_session):
    token, claims = await _grant(db_session, ["web_search"])
    action = await broker.request_action(
        db_session, claims=claims, token=token, tool_name="web.search",
        args={"query": "x"},
    )
    executor = AsyncMock(side_effect=broker.UncertainOutcome("connection dropped mid-write"))
    res = await broker.execute_action(
        db_session, action_id=action.id, claims=claims, executor=executor,
        args={"query": "x"},
    )
    assert res.status == "reconciliation_required"
    assert executor.await_count == 1

    res2 = await broker.execute_action(
        db_session, action_id=action.id, claims=claims, executor=executor,
        args={"query": "x"},
    )
    assert res2.status == "reconciliation_required"
    assert executor.await_count == 1


# ─── 12. egress target validation ─────────────────────────────────────


@pytest.mark.anyio
async def test_egress_target_validation(db_session):
    executor = AsyncMock()

    # granted host → allow (web.fetch is R1)
    token, claims = await _grant(db_session, ["http"], egress=["*.example.org"])
    ok = await broker.request_action(
        db_session, claims=claims, token=token, tool_name="web.fetch",
        args={"url": "https://a.example.org/x"},
    )
    assert ok.status == "approved"

    # host outside the allowlist → denied (egress_not_granted)
    with pytest.raises(broker.ActionDenied) as ei:
        await broker.request_action(
            db_session, claims=claims, token=token, tool_name="web.fetch",
            args={"url": "https://evil.com/x"},
        )
    assert ei.value.action.result_json["reason"] == "egress_not_granted"

    # link-local / metadata IP → blocked even with a wildcard grant
    token2, claims2 = await _grant(db_session, ["http"], egress=["*"])
    with pytest.raises(broker.ActionDenied) as ei2:
        await broker.request_action(
            db_session, claims=claims2, token=token2, tool_name="web.fetch",
            args={"url": "http://169.254.169.254/latest/meta-data"},
        )
    assert ei2.value.action.result_json["reason"] == "egress_blocked_host"

    # empty egress + network tool carrying a url → fail-closed
    token3, claims3 = await _grant(db_session, ["http"], egress=[])
    with pytest.raises(broker.ActionDenied) as ei3:
        await broker.request_action(
            db_session, claims=claims3, token=token3, tool_name="http.request",
            args={"url": "https://api.example.org/x"},
        )
    assert ei3.value.action.result_json["reason"] == "egress_not_granted"

    executor.assert_not_called()
