"""P2-A — Broker-level lease-epoch reconciliation + fenced terminal writes
(final-review Important #1/#2, PRD §Run Lease and Fencing, V07).

The P1 slice made fencing an *emergent* property of the orchestrator's step
ordering: the Broker only compared ``claims.fencing_epoch`` against the caller's
own ``expected_epoch`` — a self-referential check a stale owner passes (both
values are its own stale epoch). These tests pin the structural fence:

* the Broker reconciles the caller's epoch against the ``lab_run_leases`` row
  (the authority) on every request/execute — a taken-over owner is denied;
* approval-consume and executing-claim UPDATEs carry an epoch predicate so a
  cross-epoch consume/claim matches zero rows (TOCTOU-safe, on the same atomic
  write);
* a takeover revokes every pre-takeover grant so the stale token also dies at
  ``check_grant_active``;
* the orchestrator gates ``_fail`` / ``_succeed`` on the current epoch and sets
  ``self.fenced`` so the ``finally`` never revokes the *new* owner's grants
  (the P1 ``_fail`` StaleEpoch inversion);
* the no-lease path (epoch 0) is unchanged — every existing broker test that
  never creates a lease row keeps passing.

Scenario 1 is the proof-of-defect: it is RED against the P1 code (the stale
owner reaches the executor) and GREEN once the Broker reconciles against the
lease.
"""
from datetime import datetime, UTC
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.config import settings
from app.lab import broker, grants, ledger, leases, orchestrator
from app.models.lab_action import LabApproval, LabToolAction
from app.models.lab_grant import LabCapabilityGrant
from app.models.lab_lease import LabRunLease
from app.models.lab_run import LabRun
from app.models.lab_task import LabTask


@pytest.fixture(autouse=True)
def _grant_secret(monkeypatch):
    monkeypatch.setattr(settings, "lab_grant_secret", "test-secret", raising=False)


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _grant(db, caps, *, egress=None, fencing_epoch=0):
    """Issue + persist a real signed grant (run_id='run1') at ``fencing_epoch``."""
    token, claims = await grants.issue_run_grant(
        db, tenant_id="owner-1", task_id="task1", run_id="run1",
        agent_id="agent-1", capabilities=list(caps),
        egress=egress or [], fencing_epoch=fencing_epoch,
    )
    return token, claims


async def _count(db, model):
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


async def _approval_for(db, action_id):
    return (await db.execute(
        select(LabApproval).where(LabApproval.action_id == action_id)
    )).scalar_one()


async def _force_expire(db, run_id="run1"):
    """Age the lease into the past so the next acquire is a takeover."""
    lease = await db.get(LabRunLease, run_id)
    lease.expires_at = datetime(2000, 1, 1, tzinfo=UTC)
    lease.heartbeat_at = datetime(2000, 1, 1, tzinfo=UTC)
    await db.commit()


async def _takeover(db, *, run_id="run1", old_owner="A", new_owner="B"):
    """Acquire at epoch 0, expire, take over → returns the post-takeover epoch."""
    await leases.acquire_lease(db, run_id=run_id, owner_id=old_owner)
    await _force_expire(db, run_id)
    lease = await leases.acquire_lease(db, run_id=run_id, owner_id=new_owner)
    return lease.fencing_epoch


# ── 1. PROOF-OF-DEFECT: a stale owner directly calling the Broker is denied ──
#    RED on P1 code (expected_epoch self-reference passes → executor reachable).

@pytest.mark.anyio
async def test_stale_owner_direct_broker_request_denied(db_session):
    db = db_session
    # Lease epoch 0 (owner A); a grant minted under that epoch.
    await leases.acquire_lease(db, run_id="run1", owner_id="A")
    token_old, claims_old = await _grant(db, ["web_search"], fencing_epoch=0)

    # A takeover fences owner A: lease → epoch 1 (owner B). The old grant is NOT
    # revoked here (we call leases directly, not via the orchestrator) — so the
    # ONLY thing that can catch the stale owner is the Broker's lease
    # reconciliation, which is exactly the mechanism under test.
    await _force_expire(db, "run1")
    lease2 = await leases.acquire_lease(db, run_id="run1", owner_id="B")
    assert lease2.fencing_epoch == 1

    executor = AsyncMock()
    # The stale owner presents its OWN cached epoch (0) — the P1 self-reference
    # (claims.fencing_epoch == expected_epoch) passes, proving the defect.
    with pytest.raises(broker.ActionDenied) as ei:
        await broker.request_action(
            db, claims=claims_old, token=token_old, tool_name="web.search",
            args={"query": "x"}, expected_epoch=claims_old.fencing_epoch,
        )
    assert ei.value.reason == "stale_epoch"
    action = ei.value.action
    assert action.status == "denied"
    assert action.result_json["reason"] == "stale_epoch"
    assert action.risk_class == "NA"
    # One denied audit row, no approval row, executor never reached.
    assert await _count(db, LabToolAction) == 1
    assert await _count(db, LabApproval) == 0
    executor.assert_not_called()


# ── 2. consume/claim epoch predicates block cross-epoch execution ─────────────
#    (a) old owner execute → lease reconciliation denies (stale_epoch)
#    (b) new owner executing an old-epoch approval → atomic consume matches 0 rows

@pytest.mark.anyio
async def test_epoch_predicates_block_stale_and_cross_epoch_consume(db_session):
    db = db_session
    await leases.acquire_lease(db, run_id="run1", owner_id="A")
    token_old, claims_old = await _grant(db, ["http"], egress=["*.example.org"], fencing_epoch=0)

    async def _approved_action(url):
        act = await broker.request_action(
            db, claims=claims_old, token=token_old, tool_name="http.request",
            args={"url": url}, expected_epoch=0,
        )
        assert act.status == "waiting_approval"
        appr = await _approval_for(db, act.id)
        await broker.decide_approval(
            db, approval_id=appr.id, decider_user_id="owner-1",
            approve=True, task_owner_id="owner-1",
        )
        return act.id

    action_a = await _approved_action("https://api.example.org/a")
    action_b = await _approved_action("https://api.example.org/b")

    # Takeover → epoch 1; a fresh grant for the new owner.
    await _force_expire(db, "run1")
    lease2 = await leases.acquire_lease(db, run_id="run1", owner_id="B")
    assert lease2.fencing_epoch == 1
    token_new, claims_new = await _grant(db, ["http"], egress=["*.example.org"], fencing_epoch=1)

    executor = AsyncMock()

    # (a) The stale owner (epoch-0 claims) is denied at the lease reconciliation
    #     gate before ever reaching the consume UPDATE.
    with pytest.raises(broker.ActionDenied) as ei_old:
        await broker.execute_action(
            db, action_id=action_a, claims=claims_old, executor=executor,
            args={"url": "https://api.example.org/a"}, expected_epoch=0,
        )
    assert ei_old.value.reason == "stale_epoch"

    # (b) The current owner (epoch-1 claims) tries to consume an epoch-0 approval:
    #     the atomic consume predicate (approval.fencing_epoch == claims.epoch)
    #     matches 0 rows → not_consumable. No executor, approval left unconsumed.
    with pytest.raises(broker.ApprovalInvalid) as ei_new:
        await broker.execute_action(
            db, action_id=action_b, claims=claims_new, executor=executor,
            args={"url": "https://api.example.org/b"}, expected_epoch=1,
        )
    assert ei_new.value.reason == "not_consumable"

    executor.assert_not_called()
    appr_b = await _approval_for(db, action_b)
    assert appr_b.consumed_at is None


# ── 3. takeover revokes every pre-takeover grant (grants.revoke_grants_before_epoch) ──

@pytest.mark.anyio
async def test_revoke_grants_before_epoch_fences_stale_token(db_session):
    db = db_session
    _, claims_old1 = await _grant(db, ["web_search"], fencing_epoch=0)
    token_old2, claims_old2 = await _grant(db, ["http"], egress=["*.example.org"], fencing_epoch=0)
    _, claims_new = await _grant(db, ["web_search"], fencing_epoch=1)

    revoked = await grants.revoke_grants_before_epoch(db, run_id="run1", epoch=1)
    assert revoked == 2  # both epoch-0 grants, not the epoch-1 grant

    g1 = await db.get(LabCapabilityGrant, claims_old1.jti)
    g2 = await db.get(LabCapabilityGrant, claims_old2.jti)
    gnew = await db.get(LabCapabilityGrant, claims_new.jti)
    assert g1.revoked_at is not None
    assert g2.revoked_at is not None
    assert gnew.revoked_at is None  # current owner's grant survives

    # Idempotent: a second sweep revokes nothing.
    assert await grants.revoke_grants_before_epoch(db, run_id="run1", epoch=1) == 0

    # And the revoked token now dies at check_grant_active → Broker denies it,
    # independent of the lease gate.
    with pytest.raises(broker.ActionDenied) as ei:
        await broker.request_action(
            db, claims=claims_old2, token=token_old2, tool_name="web.fetch",
            args={"url": "https://a.example.org/x"},
        )
    assert "revoked" in ei.value.reason


# ── 4. orchestrator _fail is epoch-gated (no terminal write, no fence inversion) ──

@pytest.mark.anyio
async def test_fail_is_epoch_gated_and_sets_fenced(db_session):
    db = db_session
    db.add(LabTask(id="task1", issuer_user_id="issuer", title="t"))
    db.add(LabRun(id="run1", task_id="task1", researcher_slug="sage", status="running"))
    await db.commit()

    epoch = await _takeover(db)  # lease taken over → epoch 1
    assert epoch == 1
    # A grant the NEW owner would hold — the fence must not revoke it.
    _, claims_new = await _grant(db, ["web_search"], fencing_epoch=1)

    run = await db.get(LabRun, "run1")
    task = await db.get(LabTask, "task1")
    orch = orchestrator._Orchestrator(db, run, task)
    orch.epoch = 0  # stale: the orchestrator still thinks it owns epoch 0

    await orch._fail("boom")

    assert orch.fenced is True
    fresh = await db.get(LabRun, "run1")
    assert fresh.status == "running"  # NOT flipped to failed by the fenced owner
    # _fail performed no revoke; the finally (gated on self.fenced) will skip it.
    gnew = await db.get(LabCapabilityGrant, claims_new.jti)
    assert gnew.revoked_at is None


# ── 5. orchestrator _succeed is epoch-gated (no terminal write, no settlement) ──

@pytest.mark.anyio
async def test_succeed_is_epoch_gated_and_sets_fenced(db_session):
    db = db_session
    db.add(LabTask(id="task1", issuer_user_id="issuer", title="t"))
    db.add(LabRun(id="run1", task_id="task1", researcher_slug="sage", status="running"))
    await db.commit()

    epoch = await _takeover(db)
    assert epoch == 1

    run = await db.get(LabRun, "run1")
    task = await db.get(LabTask, "task1")
    orch = orchestrator._Orchestrator(db, run, task)
    orch.epoch = 0

    await orch._succeed()

    assert orch.fenced is True
    fresh = await db.get(LabRun, "run1")
    assert fresh.status == "running"  # never settled as succeeded


# ── 6. no-lease compatibility: the epoch-0 path is unchanged ──────────────────

@pytest.mark.anyio
async def test_no_lease_epoch_zero_request_execute_unchanged(db_session):
    db = db_session
    # No lease row — mirrors every existing broker test (epoch 0 both sides).
    token, claims = await _grant(db, ["http"], egress=["*.example.org"], fencing_epoch=0)
    args = {"url": "https://api.example.org/x"}

    action = await broker.request_action(
        db, claims=claims, token=token, tool_name="http.request", args=args,
    )
    assert action.status == "waiting_approval"
    appr = await _approval_for(db, action.id)
    await broker.decide_approval(
        db, approval_id=appr.id, decider_user_id="owner-1",
        approve=True, task_owner_id="owner-1",
    )
    executor = AsyncMock(return_value={"ok": True})
    res = await broker.execute_action(
        db, action_id=action.id, claims=claims, executor=executor, args=args,
    )
    assert res.status == "succeeded"
    assert executor.await_count == 1

    # allow-passthrough with no lease also unaffected.
    a2 = await broker.request_action(
        db, claims=claims, token=token, tool_name="web.fetch",
        args={"url": "https://a.example.org/y"},
    )
    assert a2.status == "approved"


# ── 7. _fail emit-StaleEpoch must NOT invert the fence (full execute() path) ───
#    Regression for orchestrator.py _fail: the *top* epoch gate passes (scenario
#    #4 already covers the fenced-at-entry case), but a takeover lands between the
#    gate and the run.failed append, so ``_emit`` truly raises StaleEpoch. The P1
#    code re-raised there, leaving the local ``fenced`` False → execute()'s
#    ``finally`` ran ``revoke_run_grants`` and revoked the NEW owner's grants. The
#    fix sets ``self.fenced`` and returns so the finally SKIPS revoke. This drives
#    the whole execute() path so the real finally runs and the survival of the new
#    owner's grant is a pinned behavior, not a comment.

@pytest.mark.anyio
async def test_fail_emit_stale_epoch_does_not_invert_fence(db_session, monkeypatch):
    db = db_session
    db.add(LabTask(id="task1", issuer_user_id="issuer", title="t", deliverable_kind="report"))
    db.add(LabRun(id="run1", task_id="task1", researcher_slug="sage", status="queued",
                  adapter="mock", scopes_json=["web_search"], budget_usd_cents=0))
    await db.commit()

    # Silence WS side effects; the adapter raises on start so execute() falls into
    # the terminal-failure path → _fail.
    for fn in ("_ws_task_update", "_ws_run_step", "_ws_run_approval"):
        monkeypatch.setattr(f"app.lab.orchestrator.{fn}", AsyncMock())

    class _RaisingAdapter:
        name = "mock"

        async def start(self, spec):
            raise RuntimeError("adapter boom")

    monkeypatch.setattr("app.lab.orchestrator.get_adapter", lambda name: _RaisingAdapter())

    # Inject the takeover AT the run.failed append. _fail's top gate has already
    # passed (lease still epoch 0), so this is the only fence _fail can hit — the
    # emit path under test. Bump the lease + mint the NEW owner's grant, then let
    # the real append_event run and raise StaleEpoch (expected_epoch 0 vs lease 1).
    orig_append = ledger.append_event
    new_owner: dict[str, str] = {}

    async def _takeover_then_append(db_, *, envelope, **kwargs):
        if envelope.type == "run.failed" and "jti" not in new_owner:
            lease = await db_.get(LabRunLease, "run1")
            lease.fencing_epoch = 1
            lease.owner_id = "new-owner"
            await db_.commit()
            _, claims = await grants.issue_run_grant(
                db_, tenant_id="issuer", task_id="task1", run_id="run1",
                agent_id="new", capabilities=["web_search"], fencing_epoch=1,
            )
            new_owner["jti"] = claims.jti
        return await orig_append(db_, envelope=envelope, **kwargs)

    monkeypatch.setattr(ledger, "append_event", _takeover_then_append)

    run = await db.get(LabRun, "run1")
    task = await db.get(LabTask, "task1")
    orch = orchestrator._Orchestrator(db, run, task)

    await orch.execute()

    # The takeover was actually injected at the run.failed emit, and _fail caught
    # the StaleEpoch on the *emit* path (top gate had passed) → fenced, not raised.
    assert "jti" in new_owner
    assert orch.fenced is True

    # THE regression: execute()'s finally saw self.fenced and SKIPPED revoke, so
    # the new owner's grant survives. On the P1 code the finally revoked every
    # grant on the run (revoke_run_grants), including this one.
    new_grant = await db.get(LabCapabilityGrant, new_owner["jti"])
    assert new_grant.revoked_at is None
    old_grant = await db.get(LabCapabilityGrant, orch.claims.jti)
    assert old_grant.revoked_at is None  # fenced loser revokes nothing at all

    # No refund / task settlement by the fenced loser: fail_task sits after the
    # emit and is never reached, so the task is not flipped to failed.
    task_after = await db.get(LabTask, "task1")
    assert task_after.status != "failed"

    # BOUNDARY (flagged to lead): unlike the top-gate path (scenario #4, where the
    # run is never flipped), on THIS emit path run.status="failed" is committed
    # *before* the run.failed append that detects the fence — the terminal write
    # precedes the fence point, so it is not suppressed here. The fence's
    # guarantee on this path is narrower: no revoke inversion + no refund (both
    # asserted above). Pinned so any future product gating of this write is a
    # conscious change.
    run_after = await db.get(LabRun, "run1")
    assert run_after.status == "failed"
