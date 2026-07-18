"""T7 — Orchestrator v1 end-to-end on the Mock runtime (PRD §Control Plane,
P1 exit gate / V15 backend part).

The feature flag ``settings.lab_agent_v1_enabled`` routes ``runner.run_one``
into ``orchestrator.run_one_v1``, which drives the same task/run state machine
and WS contract as the legacy path but now threads every tool intent through
grant → lease → policy/broker → ledger/outbox → budgets → (approval) →
artifact → mark_review → Compiler. These seven scenarios pin that the "Thin D"
enforcement is real on Mock while the flag-off path stays byte-for-byte legacy.

Cross-session note (mirrors test_lab_task_flow): the orchestrator + services
open their own ``async_session`` — patched here onto the shared in-memory
engine. The two approval scenarios drive the run as a background task and
resolve the approval from a separate session; the shared StaticPool connection
interleaves safely because every step releases the connection on commit.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.lab import grants, leases
from app.lab.runner import run_one
from app.lab.sandbox.base import ArtifactSpec, SandboxHandle, StepEvent
from app.models.lab_action import LabApproval, LabToolAction
from app.models.lab_artifact import LabArtifact
from app.models.lab_budget import LabRunBudget
from app.models.lab_event import LabRunEvent, OutboxEvent
from app.models.lab_grant import LabCapabilityGrant
from app.models.lab_lease import LabRunLease
from app.models.lab_run import LabRun, LabRunStep
from app.models.lab_task import LabTask
from app.models.resident import Resident
from app.models.user import User
from app.models.world_change_proposal import WorldChangeProposal
from app.services import coin_service
from app.services import lab_task_service as svc
from app.services.auth_service import create_token


# ── fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def lab_env(db_engine, monkeypatch):
    from app.config import settings
    for k, v in {
        "lab_enabled": True, "lab_adapter": "mock", "lab_creator_share": 0.2,
        "lab_platform_fee_rate": 0.1, "lab_default_budget_usd": 0.5,
        "lab_daily_tasks_per_user": 20, "lab_auto_release_hours": 72,
        "lab_task_deadline_hours": 24,
        "lab_agent_v1_enabled": True, "lab_grant_secret": "test-secret",
        "lab_approval_timeout_s": 5, "lab_egress_allowlist": ["*.example.org"],
    }.items():
        monkeypatch.setattr(settings, k, v, raising=False)
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("app.lab.runner.async_session", factory), \
         patch("app.lab.orchestrator.async_session", factory), \
         patch("app.services.lab_task_service.async_session", factory), \
         patch("app.services.lab_task_service.emit", new_callable=AsyncMock):
        yield factory


async def _seed(factory, *, issuer_balance=1000):
    async with factory() as s:
        s.add(User(id="issuer", name="Issuer", email="i@t.com", soul_coin_balance=issuer_balance))
        s.add(User(id="creator_user", name="Creator", email="c@t.com", soul_coin_balance=0))
        s.add(Resident(
            slug="sage", name="Sage", creator_id="creator_user", resident_type="npc",
            meta_json={"lab": {"access": True, "tier": "senior", "skills": ["web_search"]}},
        ))
        await s.commit()


async def _make_task(factory, *, scopes, reward_sc=100, deliverable_kind="report", title="调研任务"):
    async with factory() as s:
        task = await svc.create_task(
            s, issuer_id="issuer", title=title, brief="调研一下 X",
            scopes=scopes, reward_sc=reward_sc, deliverable_kind=deliverable_kind,
            researcher_slug="sage",
        )
        return task.id, task.accepted_run_id


async def _events(factory, run_id):
    async with factory() as s:
        rows = (await s.execute(
            select(LabRunEvent).where(LabRunEvent.run_id == run_id).order_by(LabRunEvent.seq)
        )).scalars().all()
        return [(e.type, e.seq) for e in rows]


async def _wait_for_pending_approval(factory, run_id, tries=200):
    for _ in range(tries):
        await asyncio.sleep(0.03)
        async with factory() as s:
            appr = (await s.execute(
                select(LabApproval).where(LabApproval.run_id == run_id, LabApproval.decision == "pending")
            )).scalar_one_or_none()
            if appr is not None:
                return appr.id
    return None


# ── fake adapters (patched into the orchestrator) ─────────────────────

class _FakeHandle(SandboxHandle):
    def __init__(self, spec):
        self.spec = spec


class _BaseFake:
    name = "mock"

    async def start(self, spec):
        return _FakeHandle(spec)

    async def submit_goal(self, handle, brief, scopes):
        return None

    async def approve(self, handle, approval_id, decision):
        return None

    async def collect_artifacts(self, handle):
        return [ArtifactSpec(kind="text", title="研究简报（Fake）", text_md="done", meta={"fake": True})]

    async def stop(self, handle):
        return None


class FakeHttpAdapter(_BaseFake):
    """Emits one R2 http.request tool step → pauses for approval."""

    async def step_stream(self, handle):
        yield StepEvent(phase="think", summary="需要外呼一个 API")
        yield StepEvent(
            phase="tool_call", tool="http.request", summary="POST 到 example.org",
            payload={"url": "https://api.example.org/submit", "method": "POST"}, cost_usd_cents=1,
        )
        yield StepEvent(phase="message", summary="整理结论")


class FakeShellAdapter(_BaseFake):
    """Emits a shell.exec step outside the granted scope → scope violation."""

    async def step_stream(self, handle):
        yield StepEvent(phase="think", summary="打算跑一段脚本")
        yield StepEvent(
            phase="tool_call", tool="shell.exec", summary="执行 ls",
            payload={"command": "ls -la"}, cost_usd_cents=0,
        )
        yield StepEvent(phase="message", summary="收尾")


class FakeTwoToolAdapter(_BaseFake):
    """Two web.search steps → the second trips a tool_calls budget of 1."""

    async def step_stream(self, handle):
        yield StepEvent(phase="think", summary="规划两次检索")
        yield StepEvent(phase="tool_call", tool="web.search", summary="检索甲",
                        payload={"query": "alpha"}, cost_usd_cents=1)
        yield StepEvent(phase="observation", summary="第一批结果")
        yield StepEvent(phase="tool_call", tool="web.search", summary="检索乙",
                        payload={"query": "beta"}, cost_usd_cents=1)
        yield StepEvent(phase="message", summary="收尾")


# ── 1. happy path v1 ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_happy_path_v1(lab_env):
    factory = lab_env
    await _seed(factory)
    task_id, run_id = await _make_task(factory, scopes=["web_search"], reward_sc=100)

    async with factory() as s:
        assert await coin_service.get_balance(s, "issuer") == 890  # reward+fee escrowed

    await run_one(run_id)

    async with factory() as s:
        run = await s.get(LabRun, run_id)
        assert run.status == "succeeded"
        task = await s.get(LabTask, task_id)
        assert task.status == "review"
        arts = (await s.execute(select(LabArtifact).where(LabArtifact.task_id == task_id))).scalars().all()
        assert len(arts) == 1

        grants = (await s.execute(
            select(LabCapabilityGrant).where(LabCapabilityGrant.run_id == run_id)
        )).scalars().all()
        assert len(grants) == 1 and grants[0].revoked_at is not None  # exactly one, terminal-revoked
        lease = await s.get(LabRunLease, run_id)
        assert lease is not None and lease.fencing_epoch == 0

        evs = (await s.execute(
            select(LabRunEvent).where(LabRunEvent.run_id == run_id).order_by(LabRunEvent.seq)
        )).scalars().all()
        types = [e.type for e in evs]
        assert types[0] == "run.started" and types[-1] == "run.completed"
        seqs = [e.seq for e in evs]
        assert seqs == list(range(1, len(seqs) + 1))  # gap-free 1..N

        outbox = (await s.execute(select(OutboxEvent).where(OutboxEvent.run_id == run_id))).scalars().all()
        assert len(outbox) == len(evs)  # every canonical event has an outbox row

        steps = (await s.execute(select(LabRunStep).where(LabRunStep.run_id == run_id))).scalars().all()
        assert len(steps) >= 1  # legacy-UI compat projection still lands

        budget = await s.get(LabRunBudget, run_id)
        assert budget.used_tool_calls == 1  # one Mock tool step

    # Economy settles exactly as legacy: creator 20, treasury 80, fee 10 sink.
    async with factory() as s:
        task = await svc.accept_result(s, task_id, "issuer")
        assert task.status == "completed"
    async with factory() as s:
        assert await coin_service.get_balance(s, "creator_user") == 20
        assert await coin_service.treasury_balance(s, "sage") == 80


# ── 2. approval chain v1: owner approves → run resumes → succeeds ──────

@pytest.mark.anyio
async def test_approval_chain_v1_approve(lab_env, client, monkeypatch):
    factory = lab_env
    await _seed(factory)
    monkeypatch.setattr("app.lab.orchestrator.get_adapter", lambda name: FakeHttpAdapter())
    task_id, run_id = await _make_task(factory, scopes=["http"], title="外呼")

    run_task = asyncio.create_task(run_one(run_id))
    approval_id = await _wait_for_pending_approval(factory, run_id)
    assert approval_id is not None

    async with factory() as s:
        run = await s.get(LabRun, run_id)
        assert run.status == "needs_approval"

    headers = {"Authorization": f"Bearer {create_token('issuer')}"}
    resp = await client.post(
        f"/lab/runs/{run_id}/approval",
        json={"approval_id": approval_id, "decision": True}, headers=headers,
    )
    assert resp.status_code == 200

    await run_task

    async with factory() as s:
        run = await s.get(LabRun, run_id)
        assert run.status == "succeeded"
        task = await s.get(LabTask, task_id)
        assert task.status == "review"
        appr = await s.get(LabApproval, approval_id)
        assert appr.decision == "approved" and appr.consumed_at is not None

    types = [t for t, _ in await _events(factory, run_id)]
    assert "approval.requested" in types and "approval.resolved" in types
    assert "tool.completed" in types


# ── 3. rejection chain v1: owner rejects → action denied, run completes ─

@pytest.mark.anyio
async def test_approval_chain_v1_reject(lab_env, client, monkeypatch):
    factory = lab_env
    await _seed(factory)
    monkeypatch.setattr("app.lab.orchestrator.get_adapter", lambda name: FakeHttpAdapter())
    task_id, run_id = await _make_task(factory, scopes=["http"], title="外呼")

    run_task = asyncio.create_task(run_one(run_id))
    approval_id = await _wait_for_pending_approval(factory, run_id)
    assert approval_id is not None

    headers = {"Authorization": f"Bearer {create_token('issuer')}"}
    resp = await client.post(
        f"/lab/runs/{run_id}/approval",
        json={"approval_id": approval_id, "decision": False}, headers=headers,
    )
    assert resp.status_code == 200

    await run_task

    async with factory() as s:
        run = await s.get(LabRun, run_id)
        assert run.status == "succeeded"  # a denied sensitive action does not fail the run
        task = await s.get(LabTask, task_id)
        assert task.status == "review"
        appr = await s.get(LabApproval, approval_id)
        assert appr.decision == "denied" and appr.consumed_at is None
        action = await s.get(LabToolAction, appr.action_id)
        assert action.status == "denied"  # never executed
        budget = await s.get(LabRunBudget, run_id)
        assert budget.used_tool_calls == 0 and budget.reserved_tool_calls == 0  # reservation released


# ── 4. scope violation v1: ungranted tool → run failed + refund ───────

@pytest.mark.anyio
async def test_scope_violation_v1(lab_env, monkeypatch):
    factory = lab_env
    await _seed(factory)
    monkeypatch.setattr("app.lab.orchestrator.get_adapter", lambda name: FakeShellAdapter())
    task_id, run_id = await _make_task(factory, scopes=["web_search"], reward_sc=100, title="越权")

    await run_one(run_id)

    async with factory() as s:
        run = await s.get(LabRun, run_id)
        assert run.status == "failed"
        task = await s.get(LabTask, task_id)
        assert task.status == "failed"
        assert await coin_service.get_balance(s, "issuer") == 1000  # fully refunded

        denied = (await s.execute(
            select(LabToolAction).where(LabToolAction.run_id == run_id, LabToolAction.status == "denied")
        )).scalars().all()
        assert len(denied) == 1
        assert denied[0].result_json["reason"] == "capability_not_granted"

        types = [t for t, _ in await _events(factory, run_id)]
        assert "policy.decided" in types and "run.failed" in types

        grants = (await s.execute(
            select(LabCapabilityGrant).where(LabCapabilityGrant.run_id == run_id)
        )).scalars().all()
        assert len(grants) == 1 and grants[0].revoked_at is not None


# ── 5. world change v1: success → Compiler drafts a pending proposal ──

@pytest.mark.anyio
async def test_world_change_v1(lab_env):
    factory = lab_env
    await _seed(factory)
    task_id, run_id = await _make_task(
        factory, scopes=["web_search"], deliverable_kind="world_change", title="世界探索",
    )

    await run_one(run_id)

    async with factory() as s:
        run = await s.get(LabRun, run_id)
        assert run.status == "succeeded"
        props = (await s.execute(
            select(WorldChangeProposal).where(WorldChangeProposal.origin_ref == run_id)
        )).scalars().all()
        assert len(props) == 1
        p = props[0]
        assert p.kind == "add_lore" and p.status == "pending"
        assert p.origin_ref == run_id  # not the legacy hard-coded ref
        assert p.patch_json["location_id"] == "experiment_building"

        types = [t for t, _ in await _events(factory, run_id)]
        assert "proposal.drafted" in types


# ── 6. flag=False regression: legacy path, zero v1 rows ───────────────

@pytest.mark.anyio
async def test_flag_off_legacy_regression(lab_env, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "lab_agent_v1_enabled", False)
    factory = lab_env
    await _seed(factory)
    task_id, run_id = await _make_task(factory, scopes=["web_search"], reward_sc=100)

    await run_one(run_id)

    async with factory() as s:
        run = await s.get(LabRun, run_id)
        assert run.status == "succeeded"
        task = await s.get(LabTask, task_id)
        assert task.status == "review"
        arts = (await s.execute(select(LabArtifact).where(LabArtifact.task_id == task_id))).scalars().all()
        assert len(arts) == 1
        # The rollback switch is real: not a single v1 table was written.
        assert (await s.execute(
            select(func.count()).select_from(LabRunEvent).where(LabRunEvent.run_id == run_id)
        )).scalar_one() == 0
        assert (await s.execute(
            select(func.count()).select_from(LabCapabilityGrant).where(LabCapabilityGrant.run_id == run_id)
        )).scalar_one() == 0
        assert (await s.execute(
            select(func.count()).select_from(LabToolAction).where(LabToolAction.run_id == run_id)
        )).scalar_one() == 0
        assert (await s.execute(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.run_id == run_id)
        )).scalar_one() == 0
        assert await s.get(LabRunBudget, run_id) is None
        assert await s.get(LabRunLease, run_id) is None


# ── 7. budget termination v1: tool_calls exhausted → run failed ───────

@pytest.mark.anyio
async def test_budget_termination_v1(lab_env, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "lab_budget_tool_calls", 1)
    factory = lab_env
    await _seed(factory)
    monkeypatch.setattr("app.lab.orchestrator.get_adapter", lambda name: FakeTwoToolAdapter())
    task_id, run_id = await _make_task(factory, scopes=["web_search"], reward_sc=100, title="预算")

    await run_one(run_id)

    async with factory() as s:
        run = await s.get(LabRun, run_id)
        assert run.status == "failed"
        task = await s.get(LabTask, task_id)
        assert task.status == "failed"
        assert await coin_service.get_balance(s, "issuer") == 1000  # refunded

        types = [t for t, _ in await _events(factory, run_id)]
        assert "budget.exhausted" in types and "run.failed" in types

        grants = (await s.execute(
            select(LabCapabilityGrant).where(LabCapabilityGrant.run_id == run_id)
        )).scalars().all()
        assert len(grants) == 1 and grants[0].revoked_at is not None

        budget = await s.get(LabRunBudget, run_id)
        assert budget.exhausted_dimension == "tool_calls"
        assert budget.used_tool_calls == 1  # only the first step settled


# ── 8. held lease is fenced, not a refundable failure ─────────────────

@pytest.mark.anyio
async def test_held_lease_is_fenced_not_failed(lab_env):
    """A concurrent owner already holds a live lease (queue redelivery). The
    v1 orchestrator must abandon the run untouched — it must NOT flip the run to
    running, refund the task, or revoke the holder's grant. Regression guard for
    the review finding that ``LeaseError('held')`` fell into the generic failure
    path and inverted fencing."""
    factory = lab_env
    await _seed(factory)
    task_id, run_id = await _make_task(factory, scopes=["web_search"], reward_sc=100, title="争用")

    # Another owner grabs the lease + mints a grant first (the true holder).
    async with factory() as s:
        await leases.acquire_lease(s, run_id=run_id, owner_id="holder-owner")
        _, holder_claims = await grants.issue_run_grant(
            s, tenant_id="issuer", task_id=task_id, run_id=run_id,
            agent_id="holder", capabilities=["web_search"],
        )
        holder_jti = holder_claims.jti

    await run_one(run_id)  # flag on → orchestrator → acquire_lease raises held

    async with factory() as s:
        run = await s.get(LabRun, run_id)
        assert run.status == "queued"  # never flipped to running
        task = await s.get(LabTask, task_id)
        assert task.status == "assigned"  # unchanged (create_task left it assigned)
        assert await coin_service.get_balance(s, "issuer") == 890  # NOT refunded (still escrowed)

        holder_grant = await s.get(LabCapabilityGrant, holder_jti)
        assert holder_grant.revoked_at is None  # the holder's grant survives

        # No terminal state / events were written by the fenced loser.
        assert (await s.execute(
            select(func.count()).select_from(LabRunEvent).where(LabRunEvent.run_id == run_id)
        )).scalar_one() == 0
        lease = await s.get(LabRunLease, run_id)
        assert lease.owner_id == "holder-owner" and lease.fencing_epoch == 0


# ── 9. V12 artifact integrity fields land on the happy path (P2-B) ────

@pytest.mark.anyio
async def test_happy_path_v1_artifact_has_integrity_fields(lab_env):
    """finalize_artifact runs inside _succeed before commit — the artifact row
    a happy-path run produces must already carry its tenant/digest/expiry, not
    just the legacy kind/title/uri/text_md/meta_json fields."""
    factory = lab_env
    await _seed(factory)
    task_id, run_id = await _make_task(factory, scopes=["web_search"], reward_sc=100)

    await run_one(run_id)

    async with factory() as s:
        arts = (await s.execute(select(LabArtifact).where(LabArtifact.task_id == task_id))).scalars().all()
        assert len(arts) == 1
        a = arts[0]
        assert a.tenant_id == "issuer"
        assert a.sha256 is not None and len(a.sha256) == 64
        assert a.expires_at is not None
