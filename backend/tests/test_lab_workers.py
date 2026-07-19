"""T7 — P4 specialist workers: role-scoped depth-1 delegation on top of the
(already-tested) grant-attenuation primitive.

Covers the P4 exit gate (PRD §Delivery Plan): child grant is a strict subset,
concurrency cap of 3, independent read-only Verifier, World Cartographer that
can never apply, cancellation revokes children, and the Archivist writes a
redacted memory candidate. Aggregate budget persistence is inherent — every
worker shares the run's single LabRunBudget row.
"""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.config import settings
from app.lab import budgets, grants, workers
from app.models.lab_grant import LabCapabilityGrant
from app.models.lab_worker_attempt import LabWorkerAttempt

PARENT_CAPS = ["web_search", "http", "browse", "code", "world_propose"]
PARENT_EGRESS = ["*.wikipedia.org"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def env(db_engine, monkeypatch):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    import app.database as database
    monkeypatch.setattr(database, "async_session", factory)
    async with factory() as db:
        await budgets.init_run_budget(db, run_id="run-1", tenant_id="tenant-1")
        token, parent = await grants.issue_run_grant(
            db, tenant_id="tenant-1", task_id="task-1", run_id="run-1",
            agent_id="principal", capabilities=list(PARENT_CAPS), egress=list(PARENT_EGRESS),
        )
    return factory, parent


# ── role templates ──────────────────────────────────────────────────────

def test_role_capability_templates():
    caps = workers.role_capabilities
    assert caps("scout") == {"web_search", "http", "browse"}
    assert caps("builder") == {"code"}
    assert caps("verifier") == {"code"}          # test execution only
    assert caps("archivist") == set()            # no tool execution
    assert caps("world_cartographer") == {"world_propose"}
    # a Verifier is read-only test exec — it must not carry fs.write
    assert "fs.write" not in workers.ROLE_TEMPLATES["verifier"].tools
    assert {"code.run", "shell.exec"} == workers.ROLE_TEMPLATES["verifier"].tools
    # the Cartographer proposes, never applies
    assert "world.apply" not in workers.ROLE_TEMPLATES["world_cartographer"].tools
    assert "world_apply" not in caps("world_cartographer")


def test_unknown_role_rejected():
    with pytest.raises(workers.WorkerRoleError):
        workers.role_capabilities("operator")


# ── V03: child grant is a strict subset ───────────────────────────────────

@pytest.mark.anyio
async def test_delegate_worker_is_strict_attenuation(env):
    factory, parent = env
    async with factory() as db:
        _tok, child = await workers.delegate_worker(
            db, parent_claims=parent, role="scout", agent_id="scout-1",
        )
    assert child.depth == 1
    assert child.parent_jti == parent.jti
    assert child.tenant_id == parent.tenant_id and child.run_id == parent.run_id
    assert set(child.capabilities).issubset(parent.capabilities)
    assert set(child.capabilities) == {"web_search", "http", "browse"}
    assert set(child.egress).issubset(parent.egress)
    for dim, v in child.budgets.items():
        assert v <= parent.budgets.get(dim, 0)
    assert child.exp <= parent.exp


@pytest.mark.anyio
async def test_worker_cannot_escalate_beyond_parent(env):
    factory, _parent = env
    # a parent that only holds web_search
    async with factory() as db:
        _t, narrow = await grants.issue_run_grant(
            db, tenant_id="tenant-1", task_id="task-1", run_id="run-1",
            agent_id="principal2", capabilities=["web_search"], egress=[],
        )
    async with factory() as db:
        # Builder needs `code`, which the parent lacks → the child gets only the
        # intersection (nothing here), never an escalation.
        _tok, child = await workers.delegate_worker(
            db, parent_claims=narrow, role="builder", agent_id="builder-x",
        )
    assert "code" not in child.capabilities
    assert set(child.capabilities).issubset(narrow.capabilities)


# ── concurrency cap of 3 ───────────────────────────────────────────────────

@pytest.mark.anyio
async def test_concurrency_cap_three(env):
    factory, parent = env
    assert settings.lab_budget_active_workers == 3
    for i, role in enumerate(["scout", "builder", "verifier"]):
        async with factory() as db:
            await workers.delegate_worker(db, parent_claims=parent, role=role, agent_id=f"w{i}")
    async with factory() as db:
        assert await workers.active_worker_count(db, "run-1") == 3
        # the 4th is refused — fail-closed, but the RUN is not terminated
        with pytest.raises(workers.WorkerLimitError):
            await workers.delegate_worker(db, parent_claims=parent, role="archivist", agent_id="w3")
    async with factory() as db:
        assert await budgets.is_exhausted(db, "run-1") is None  # cap != budget kill


@pytest.mark.anyio
async def test_finish_worker_frees_a_slot(env):
    factory, parent = env
    jtis = []
    for i, role in enumerate(["scout", "builder", "verifier"]):
        async with factory() as db:
            _t, c = await workers.delegate_worker(db, parent_claims=parent, role=role, agent_id=f"w{i}")
            jtis.append(c.jti)
    async with factory() as db:
        await workers.finish_worker(db, jti=jtis[0])
        assert await workers.active_worker_count(db, "run-1") == 2
    async with factory() as db:  # a slot is free again
        _t, c = await workers.delegate_worker(db, parent_claims=parent, role="archivist", agent_id="w3")
        assert c.depth == 1


# ── cancellation / cleanup ────────────────────────────────────────────────

@pytest.mark.anyio
async def test_cancel_revokes_all_children(env):
    factory, parent = env
    for i, role in enumerate(["scout", "verifier"]):
        async with factory() as db:
            await workers.delegate_worker(db, parent_claims=parent, role=role, agent_id=f"w{i}")
    async with factory() as db:
        assert await workers.active_worker_count(db, "run-1") == 2
        await grants.revoke_run_grants(db, "run-1")
    async with factory() as db:
        assert await workers.active_worker_count(db, "run-1") == 0
        # every child grant row is revoked
        rows = (await db.execute(
            select(LabCapabilityGrant).where(
                LabCapabilityGrant.run_id == "run-1", LabCapabilityGrant.parent_jti.isnot(None)
            )
        )).scalars().all()
        assert rows and all(r.revoked_at is not None for r in rows)


# ── aggregate budget shared across parent + children ──────────────────────

@pytest.mark.anyio
async def test_aggregate_budget_is_shared_run_row(env):
    factory, parent = env
    async with factory() as db:
        _t, child = await workers.delegate_worker(db, parent_claims=parent, role="builder", agent_id="b1")
    # parent spends 1 tool_call, child spends 2 — all accrue to ONE run budget
    async with factory() as db:
        await budgets.spend(db, run_id="run-1", dimension="tool_calls", amount=1)
    async with factory() as db:
        await budgets.spend(db, run_id="run-1", dimension="tool_calls", amount=2)
    async with factory() as db:
        snap = await budgets.snapshot(db, "run-1")
    assert snap["tool_calls"]["used"] == 3


# ── Phase 6: durable Mock child execution + finish lifecycle ──────────────

@pytest.mark.anyio
async def test_delegate_creates_durable_attempt(env):
    factory, parent = env
    async with factory() as db:
        _t, child = await workers.delegate_worker(
            db, parent_claims=parent, role="scout", agent_id="s1", sub_goal="find sources")
    async with factory() as db:
        a = (await db.execute(
            select(LabWorkerAttempt).where(LabWorkerAttempt.grant_jti == child.jti)
        )).scalar_one()
        assert a.role == "scout" and a.status == "running"
        assert a.child_runtime_id and a.sub_goal_hash and len(a.sub_goal_hash) == 64  # content-free


@pytest.mark.anyio
async def test_execute_on_mock_digest_is_server_computed(env):
    """The result digest is derived server-side from role+child identity, so a
    child cannot spoof a 'success' the parent would trust."""
    factory, parent = env
    async with factory() as db:
        _t, child = await workers.delegate_worker(db, parent_claims=parent, role="verifier", agent_id="v1")
        r1 = await workers.execute_worker_on_mock(db, child_claims=child, role="verifier", sub_goal="check")
        assert r1.status == "succeeded" and r1.verdict == "pass"
        assert r1.result_digest and len(r1.result_digest) == 64
        # Deterministic + tied to the child identity (not the child's own report).
        r2 = await workers.execute_worker_on_mock(db, child_claims=child, role="verifier", sub_goal="check")
        assert r2.result_digest == r1.result_digest


@pytest.mark.anyio
async def test_finish_worker_is_idempotent(env):
    """A second finish_worker on an already-terminal attempt must not release the
    slot twice (which would let an extra worker in)."""
    factory, parent = env
    jtis = []
    for i, role in enumerate(["scout", "builder", "verifier"]):
        async with factory() as db:
            _t, c = await workers.delegate_worker(db, parent_claims=parent, role=role, agent_id=f"w{i}")
            jtis.append(c.jti)
    async with factory() as db:
        await workers.finish_worker(db, jti=jtis[0], status="succeeded", result_digest="d")
        await workers.finish_worker(db, jti=jtis[0], status="succeeded", result_digest="d")  # double
    # Only ONE slot freed: 3 taken - 1 = 2 running; a 4th fits, a 5th does not.
    async with factory() as db:
        _t, c4 = await workers.delegate_worker(db, parent_claims=parent, role="archivist", agent_id="w4")
    async with factory() as db:
        with pytest.raises(workers.WorkerLimitError):
            await workers.delegate_worker(db, parent_claims=parent, role="archivist", agent_id="w5")
    async with factory() as db:
        a = (await db.execute(
            select(LabWorkerAttempt).where(LabWorkerAttempt.grant_jti == jtis[0])
        )).scalar_one()
        assert a.status == "succeeded" and a.ended_at is not None


@pytest.mark.anyio
async def test_reconcile_worker_slots_heals_leak(env):
    factory, parent = env
    async with factory() as db:
        await workers.delegate_worker(db, parent_claims=parent, role="scout", agent_id="s1")
    # Simulate a leaked counter (a crashed supervisor left it high).
    from app.redis_client import get_redis
    await get_redis().set(workers._slot_key("run-1"), 99)
    async with factory() as db:
        live = await workers.reconcile_worker_slots(db, "run-1")
    assert live == 1
    assert int(await get_redis().get(workers._slot_key("run-1"))) == 1


# ── Archivist: redacted memory candidate ──────────────────────────────────

@pytest.mark.anyio
async def test_archivist_summary_redacts_and_writes_memory(env):
    factory, _parent = env
    with patch("app.memory.service.MemoryService.add_memory", new=AsyncMock()) as add_mem:
        async with factory() as db:
            summary = await workers.archivist_summary(
                db, run_id="run-1", principal_id="res-1",
                accepted_artifacts=[{"title": "report", "kind": "file", "provenance": "verifier",
                                     "sha256": "abc"}],
                redacted_events=["contacted admin at secret@example.com about api_key=sk-live-123"],
            )
    # secret + email are redacted out of the candidate
    assert "secret@example.com" not in summary
    assert "sk-live-123" not in summary
    assert "report" in summary
    # a memory candidate was written for the principal resident
    add_mem.assert_awaited_once()
    kwargs = add_mem.await_args.kwargs
    args = add_mem.await_args.args
    written = " ".join(str(a) for a in args) + " " + " ".join(f"{k}={v}" for k, v in kwargs.items())
    assert "secret@example.com" not in written and "sk-live-123" not in written
