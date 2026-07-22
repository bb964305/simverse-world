"""T5 — eight-dimension hard budgets (PRD §Hard Budgets, V10). The Broker
pre-commits spend via ``reserve`` before a tool call runs so two concurrent
calls can't both slip under the limit; ``confirm``/``release`` settle a
reservation once the outcome is known; ``spend`` debits directly for
streamed usage (model tokens) with no prior reservation. Exhaustion is
terminal for the run: the Broker revokes every grant and leaves a denied
audit row.
"""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.lab import broker, budgets, grants
from app.models.lab_action import LabApproval, LabToolAction
from app.models.lab_budget import LabRunBudget


@pytest.fixture(autouse=True)
def _grant_secret(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "lab_grant_secret", "test-secret", raising=False)


# ─── 1. init is idempotent; limits default from settings ──────────────


@pytest.mark.anyio
async def test_init_run_budget_idempotent_with_settings_defaults(db_session):
    row1 = await budgets.init_run_budget(db_session, run_id="run-a", tenant_id="owner-1")
    row2 = await budgets.init_run_budget(db_session, run_id="run-a", tenant_id="owner-1")
    assert row1.run_id == row2.run_id

    count = (await db_session.execute(select(func.count()).select_from(LabRunBudget))).scalar_one()
    assert count == 1
    assert row1.limit_model_tokens == 200_000


# ─── 2. each dimension exhausts independently ──────────────────────────


@pytest.mark.anyio
@pytest.mark.parametrize("dimension", budgets.DIMENSIONS)
async def test_reserve_exhausts_independently_per_dimension(db_session, dimension):
    await budgets.init_run_budget(db_session, run_id="run-b", tenant_id="owner-1",
                                   limits={dimension: 2})
    await budgets.reserve(db_session, run_id="run-b", dimension=dimension)
    await budgets.reserve(db_session, run_id="run-b", dimension=dimension)

    with pytest.raises(budgets.BudgetExhausted) as ei:
        await budgets.reserve(db_session, run_id="run-b", dimension=dimension)
    assert ei.value.dimension == dimension

    row = await db_session.get(LabRunBudget, "run-b")
    assert row.exhausted_dimension == dimension

    # A different dimension is unaffected by this one's exhaustion.
    other = next(d for d in budgets.DIMENSIONS if d != dimension)
    await budgets.reserve(db_session, run_id="run-b", dimension=other)  # must not raise


# ─── 3. reserve -> confirm settles used; reserve -> release refunds ────


@pytest.mark.anyio
async def test_reserve_confirm_and_reserve_release(db_session):
    await budgets.init_run_budget(db_session, run_id="run-c", tenant_id="owner-1")

    await budgets.reserve(db_session, run_id="run-c", dimension="tool_calls")
    await budgets.confirm(db_session, run_id="run-c", dimension="tool_calls")
    row = await db_session.get(LabRunBudget, "run-c")
    assert row.used_tool_calls == 1
    assert row.reserved_tool_calls == 0

    await budgets.reserve(db_session, run_id="run-c", dimension="tool_calls")
    await budgets.release(db_session, run_id="run-c", dimension="tool_calls")
    row = await db_session.get(LabRunBudget, "run-c")
    assert row.used_tool_calls == 1  # untouched by the release
    assert row.reserved_tool_calls == 0


# ─── 3b. confirm/release are atomic — no lost update across sessions ──


@pytest.mark.anyio
async def test_confirm_atomic_no_lost_update_across_sessions(db_engine):
    """Two callers each hold their own independently-loaded (and, after the
    other commits, stale) copy of the row and each confirm their own unit.
    A Python-side read-modify-write would have the second caller compute its
    new ``used`` off its own stale pre-commit read and clobber the first
    caller's delta (final ``used`` == 1, wrong). The atomic UPDATE computes
    the delta in SQL off the row's current committed value, so both land."""
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s0:
        await budgets.init_run_budget(s0, run_id="run-confirm-race", tenant_id="owner-1")
        await budgets.reserve(s0, run_id="run-confirm-race", dimension="tool_calls", amount=2)

    s1, s2 = factory(), factory()
    try:
        row1 = await s1.get(LabRunBudget, "run-confirm-race")
        row2 = await s2.get(LabRunBudget, "run-confirm-race")
        assert row1.reserved_tool_calls == 2 and row2.reserved_tool_calls == 2  # both stale-equal

        await budgets.confirm(s1, run_id="run-confirm-race", dimension="tool_calls", reserved=1, actual=1)
        await budgets.confirm(s2, run_id="run-confirm-race", dimension="tool_calls", reserved=1, actual=1)
    finally:
        await s1.close()
        await s2.close()

    async with factory() as s3:
        row = await s3.get(LabRunBudget, "run-confirm-race")
        assert row.used_tool_calls == 2      # both confirms landed, not just the last writer
        assert row.reserved_tool_calls == 0


@pytest.mark.anyio
async def test_release_atomic_no_lost_update_across_sessions(db_engine):
    """Same lost-update shape as the confirm test above, for release."""
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s0:
        await budgets.init_run_budget(s0, run_id="run-release-race", tenant_id="owner-1")
        await budgets.reserve(s0, run_id="run-release-race", dimension="tool_calls", amount=3)

    s1, s2 = factory(), factory()
    try:
        row1 = await s1.get(LabRunBudget, "run-release-race")
        row2 = await s2.get(LabRunBudget, "run-release-race")
        assert row1.reserved_tool_calls == 3 and row2.reserved_tool_calls == 3  # both stale-equal

        await budgets.release(s1, run_id="run-release-race", dimension="tool_calls", amount=1)
        await budgets.release(s2, run_id="run-release-race", dimension="tool_calls", amount=1)
    finally:
        await s1.close()
        await s2.close()

    async with factory() as s3:
        row = await s3.get(LabRunBudget, "run-release-race")
        assert row.reserved_tool_calls == 1  # 3 - 1 - 1, not clobbered back to 2


# ─── 4. spend debits directly, no prior reservation required ──────────


@pytest.mark.anyio
async def test_spend_direct_debit_and_over_limit(db_session):
    await budgets.init_run_budget(db_session, run_id="run-d", tenant_id="owner-1",
                                   limits={"model_tokens": 100})
    await budgets.spend(db_session, run_id="run-d", dimension="model_tokens", amount=60)
    await budgets.spend(db_session, run_id="run-d", dimension="model_tokens", amount=40)
    row = await db_session.get(LabRunBudget, "run-d")
    assert row.used_model_tokens == 100

    with pytest.raises(budgets.BudgetExhausted) as ei:
        await budgets.spend(db_session, run_id="run-d", dimension="model_tokens", amount=1)
    assert ei.value.dimension == "model_tokens"
    row = await db_session.get(LabRunBudget, "run-d")
    assert row.used_model_tokens == 100  # rejected spend leaves the counter untouched


# ─── 5. limit 0 means unlimited ────────────────────────────────────────


@pytest.mark.anyio
async def test_zero_limit_dimension_is_unlimited(db_session):
    await budgets.init_run_budget(db_session, run_id="run-e", tenant_id="owner-1",
                                   limits={"egress_requests": 0})
    for _ in range(3):
        await budgets.reserve(db_session, run_id="run-e", dimension="egress_requests", amount=1000)
    row = await db_session.get(LabRunBudget, "run-e")
    assert row.reserved_egress_requests == 3000


# ─── 6. counters persist across a resume (new session, same engine) ───


@pytest.mark.anyio
async def test_counters_survive_resume_new_session(db_engine):
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s1:
        await budgets.init_run_budget(s1, run_id="run-f", tenant_id="owner-1")
        await budgets.reserve(s1, run_id="run-f", dimension="tool_calls", amount=3)
        await budgets.confirm(s1, run_id="run-f", dimension="tool_calls", reserved=1)

    async with factory() as s2:
        row = await budgets.init_run_budget(s2, run_id="run-f", tenant_id="owner-1")
        assert row.used_tool_calls == 1
        assert row.reserved_tool_calls == 2


# ─── 7. broker integration: exhaustion revokes grants + denies ────────


@pytest.mark.anyio
async def test_broker_reserve_exhaustion_revokes_grants_and_denies(db_session):
    token, claims = await grants.issue_run_grant(
        db_session, tenant_id="owner-1", task_id="task1", run_id="run-g",
        agent_id="agent-1", capabilities=["web_search"],
    )
    await budgets.init_run_budget(db_session, run_id="run-g", tenant_id="owner-1",
                                   limits={"tool_calls": 1})

    a1 = await broker.request_action(
        db_session, claims=claims, token=token, tool_name="web.search", args={"query": "x"},
    )
    assert a1.status == "approved"

    with pytest.raises(budgets.BudgetExhausted):
        await broker.request_action(
            db_session, claims=claims, token=token, tool_name="web.search", args={"query": "y"},
        )

    # The run's grant(s) are all revoked — a subsequent liveness check raises.
    with pytest.raises(grants.GrantError):
        await grants.check_grant_active(db_session, claims)

    denied = (await db_session.execute(
        select(LabToolAction).where(LabToolAction.status == "denied")
    )).scalars().all()
    assert len(denied) == 1
    assert "budget_exhausted" in denied[0].result_json["reason"]


# ─── 8. UncertainOutcome keeps the reservation (reconciliation, no drop) ─


@pytest.mark.anyio
async def test_uncertain_outcome_does_not_release_reservation(db_session):
    token, claims = await grants.issue_run_grant(
        db_session, tenant_id="owner-1", task_id="task1", run_id="run-h",
        agent_id="agent-1", capabilities=["web_search"],
    )
    await budgets.init_run_budget(db_session, run_id="run-h", tenant_id="owner-1")

    action = await broker.request_action(
        db_session, claims=claims, token=token, tool_name="web.search", args={"query": "x"},
    )
    executor = AsyncMock(side_effect=broker.UncertainOutcome("connection dropped mid-write"))
    result = await broker.execute_action(
        db_session, action_id=action.id, claims=claims, executor=executor, args={"query": "x"},
    )
    assert result.status == "reconciliation_required"

    snap = await budgets.snapshot(db_session, "run-h")
    assert snap["tool_calls"]["reserved"] == 1
    assert snap["tool_calls"]["used"] == 0


@pytest.mark.anyio
async def test_action_terminal_state_rolls_back_with_budget_settlement_failure(
    db_session, monkeypatch
):
    token, claims = await grants.issue_run_grant(
        db_session,
        tenant_id="owner-1",
        task_id="task-atomic-settlement",
        run_id="run-atomic-settlement",
        agent_id="agent-1",
        capabilities=["browse"],
        egress=["*.example.org"],
    )
    await budgets.init_run_budget(
        db_session,
        run_id="run-atomic-settlement",
        tenant_id="owner-1",
        limits={
            "tool_calls": 5,
            "egress_requests": 5,
            "egress_bytes": 10_000,
        },
    )
    args = {"url": "https://docs.example.org/result"}
    action = await broker.request_action(
        db_session,
        claims=claims,
        token=token,
        tool_name="browser.navigate",
        args=args,
    )
    action_id = action.id
    effects = 0

    async def executor(tool_name, received_args):
        nonlocal effects
        effects += 1
        return {"body": "broker result"}

    original_settlement = budgets.stage_action_settlement

    async def fail_after_staging(*args, **kwargs):
        await original_settlement(*args, **kwargs)
        raise RuntimeError("injected settlement transaction failure")

    monkeypatch.setattr(budgets, "stage_action_settlement", fail_after_staging)
    with pytest.raises(RuntimeError, match="settlement transaction failure"):
        await broker.execute_action(
            db_session,
            action_id=action_id,
            claims=claims,
            executor=executor,
            args=args,
        )

    db_session.expire_all()
    stored = await db_session.get(LabToolAction, action_id)
    budget = await db_session.get(LabRunBudget, "run-atomic-settlement")
    assert stored.status == "executing"
    assert stored.result_json is None
    assert budget.used_tool_calls == 0
    assert budget.reserved_tool_calls == 1
    assert budget.used_egress_requests == 0
    assert budget.reserved_egress_requests == 1
    assert budget.used_egress_bytes == 0

    with pytest.raises(broker.ApprovalInvalid, match="already_executing"):
        await broker.execute_action(
            db_session,
            action_id=action_id,
            claims=claims,
            executor=executor,
            args=args,
        )
    assert effects == 1


@pytest.mark.anyio
async def test_broker_action_reservation_is_atomic_on_partial_exhaustion(db_session):
    token, claims = await grants.issue_run_grant(
        db_session,
        tenant_id="owner-1",
        task_id="task-partial-reservation",
        run_id="run-partial-reservation",
        agent_id="agent-1",
        capabilities=["browse"],
        egress=["*.example.org"],
    )
    await budgets.init_run_budget(
        db_session,
        run_id="run-partial-reservation",
        tenant_id="owner-1",
        limits={"tool_calls": 5, "egress_requests": 1},
    )
    await budgets.reserve(
        db_session,
        run_id="run-partial-reservation",
        dimension="egress_requests",
    )

    with pytest.raises(budgets.BudgetExhausted) as exc_info:
        await broker.request_action(
            db_session,
            claims=claims,
            token=token,
            tool_name="browser.navigate",
            args={"url": "https://docs.example.org/blocked"},
        )

    assert exc_info.value.dimension == "egress_requests"
    budget = await db_session.get(LabRunBudget, "run-partial-reservation")
    assert budget.reserved_tool_calls == 0
    assert budget.reserved_egress_requests == 1


@pytest.mark.anyio
async def test_approval_denial_settles_reservations_in_decision_transaction(db_session):
    token, claims = await grants.issue_run_grant(
        db_session,
        tenant_id="owner-1",
        task_id="task-approval-denial",
        run_id="run-approval-denial",
        agent_id="agent-1",
        capabilities=["http"],
        egress=["*.example.org"],
    )
    await budgets.init_run_budget(
        db_session,
        run_id="run-approval-denial",
        tenant_id="owner-1",
    )
    action = await broker.request_action(
        db_session,
        claims=claims,
        token=token,
        tool_name="http.request",
        args={"url": "https://api.example.org/data"},
    )
    approval = await db_session.scalar(
        select(LabApproval).where(LabApproval.action_id == action.id)
    )
    action_id = action.id

    await broker.decide_approval(
        db_session,
        approval_id=approval.id,
        decider_user_id="owner-1",
        approve=False,
        task_owner_id="owner-1",
    )

    db_session.expire_all()
    stored = await db_session.get(LabToolAction, action_id)
    budget = await db_session.get(LabRunBudget, "run-approval-denial")
    assert stored.status == "denied"
    assert stored.result_json == {"reason": "approval_denied"}
    assert budget.reserved_tool_calls == 0
    assert budget.reserved_egress_requests == 0


@pytest.mark.anyio
async def test_approval_timeout_settles_reservations_atomically(db_session):
    token, claims = await grants.issue_run_grant(
        db_session,
        tenant_id="owner-1",
        task_id="task-approval-timeout",
        run_id="run-approval-timeout",
        agent_id="agent-1",
        capabilities=["http"],
        egress=["*.example.org"],
    )
    await budgets.init_run_budget(
        db_session,
        run_id="run-approval-timeout",
        tenant_id="owner-1",
    )
    action = await broker.request_action(
        db_session,
        claims=claims,
        token=token,
        tool_name="http.request",
        args={"url": "https://api.example.org/data"},
    )
    approval = await db_session.scalar(
        select(LabApproval).where(LabApproval.action_id == action.id)
    )
    action_id = action.id
    approval_id = approval.id
    approval.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    with pytest.raises(broker.ApprovalInvalid, match="expired"):
        await broker.decide_approval(
            db_session,
            approval_id=approval.id,
            decider_user_id="owner-1",
            approve=True,
            task_owner_id="owner-1",
        )

    db_session.expire_all()
    stored = await db_session.get(LabToolAction, action_id)
    stored_approval = await db_session.get(LabApproval, approval_id)
    budget = await db_session.get(LabRunBudget, "run-approval-timeout")
    assert stored.status == "denied"
    assert stored_approval.decision == "expired"
    assert budget.reserved_tool_calls == 0
    assert budget.reserved_egress_requests == 0


@pytest.mark.anyio
async def test_execute_time_denial_settles_reservations_with_action(db_session):
    token, claims = await grants.issue_run_grant(
        db_session,
        tenant_id="owner-1",
        task_id="task-execute-denial",
        run_id="run-execute-denial",
        agent_id="agent-1",
        capabilities=["browse"],
        egress=["*.example.org"],
    )
    await budgets.init_run_budget(
        db_session,
        run_id="run-execute-denial",
        tenant_id="owner-1",
    )
    args = {"url": "https://docs.example.org/revoked"}
    action = await broker.request_action(
        db_session,
        claims=claims,
        token=token,
        tool_name="browser.navigate",
        args=args,
    )
    action_id = action.id
    await grants.revoke_run_grants(db_session, claims.run_id)

    with pytest.raises(broker.ActionDenied, match="grant_inactive"):
        await broker.execute_action(
            db_session,
            action_id=action.id,
            claims=claims,
            executor=AsyncMock(),
            args=args,
        )

    db_session.expire_all()
    stored = await db_session.get(LabToolAction, action_id)
    budget = await db_session.get(LabRunBudget, "run-execute-denial")
    assert stored.status == "denied"
    assert budget.reserved_tool_calls == 0
    assert budget.reserved_egress_requests == 0


@pytest.mark.anyio
async def test_approved_but_expired_action_settles_reservations(db_session):
    token, claims = await grants.issue_run_grant(
        db_session,
        tenant_id="owner-1",
        task_id="task-approved-expired",
        run_id="run-approved-expired",
        agent_id="agent-1",
        capabilities=["http"],
        egress=["*.example.org"],
    )
    await budgets.init_run_budget(
        db_session,
        run_id="run-approved-expired",
        tenant_id="owner-1",
    )
    args = {"url": "https://api.example.org/expired"}
    action = await broker.request_action(
        db_session,
        claims=claims,
        token=token,
        tool_name="http.request",
        args=args,
    )
    approval = await db_session.scalar(
        select(LabApproval).where(LabApproval.action_id == action.id)
    )
    action_id = action.id
    approval_id = approval.id
    await broker.decide_approval(
        db_session,
        approval_id=approval.id,
        decider_user_id="owner-1",
        approve=True,
        task_owner_id="owner-1",
    )
    approval.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    with pytest.raises(broker.ApprovalInvalid, match="expired"):
        await broker.execute_action(
            db_session,
            action_id=action.id,
            claims=claims,
            executor=AsyncMock(),
            args=args,
        )

    db_session.expire_all()
    stored = await db_session.get(LabToolAction, action_id)
    stored_approval = await db_session.get(LabApproval, approval_id)
    budget = await db_session.get(LabRunBudget, "run-approved-expired")
    assert stored.status == "denied"
    assert stored_approval.decision == "expired"
    assert budget.reserved_tool_calls == 0
    assert budget.reserved_egress_requests == 0


@pytest.mark.anyio
async def test_approval_denial_rolls_back_with_settlement_failure(
    db_session, monkeypatch
):
    token, claims = await grants.issue_run_grant(
        db_session,
        tenant_id="owner-1",
        task_id="task-denial-rollback",
        run_id="run-denial-rollback",
        agent_id="agent-1",
        capabilities=["http"],
        egress=["*.example.org"],
    )
    await budgets.init_run_budget(
        db_session,
        run_id="run-denial-rollback",
        tenant_id="owner-1",
    )
    action = await broker.request_action(
        db_session,
        claims=claims,
        token=token,
        tool_name="http.request",
        args={"url": "https://api.example.org/rollback"},
    )
    approval = await db_session.scalar(
        select(LabApproval).where(LabApproval.action_id == action.id)
    )
    action_id = action.id
    approval_id = approval.id
    original_settlement = budgets.stage_action_settlement

    async def fail_after_staging(*args, **kwargs):
        await original_settlement(*args, **kwargs)
        raise RuntimeError("injected approval settlement failure")

    monkeypatch.setattr(budgets, "stage_action_settlement", fail_after_staging)
    with pytest.raises(RuntimeError, match="approval settlement failure"):
        await broker.decide_approval(
            db_session,
            approval_id=approval_id,
            decider_user_id="owner-1",
            approve=False,
            task_owner_id="owner-1",
        )

    stored = await db_session.get(LabToolAction, action_id)
    stored_approval = await db_session.get(LabApproval, approval_id)
    budget = await db_session.get(LabRunBudget, "run-denial-rollback")
    assert stored.status == "waiting_approval"
    assert stored.result_json is None
    assert stored_approval.decision == "pending"
    assert budget.reserved_tool_calls == 1
    assert budget.reserved_egress_requests == 1


@pytest.mark.anyio
async def test_execute_denial_rolls_back_with_settlement_failure(
    db_session, monkeypatch
):
    token, claims = await grants.issue_run_grant(
        db_session,
        tenant_id="owner-1",
        task_id="task-execute-denial-rollback",
        run_id="run-execute-denial-rollback",
        agent_id="agent-1",
        capabilities=["browse"],
        egress=["*.example.org"],
    )
    await budgets.init_run_budget(
        db_session,
        run_id="run-execute-denial-rollback",
        tenant_id="owner-1",
    )
    args = {"url": "https://docs.example.org/rollback"}
    action = await broker.request_action(
        db_session,
        claims=claims,
        token=token,
        tool_name="browser.navigate",
        args=args,
    )
    action_id = action.id
    await grants.revoke_run_grants(db_session, claims.run_id)
    original_settlement = budgets.stage_action_settlement

    async def fail_after_staging(*args, **kwargs):
        await original_settlement(*args, **kwargs)
        raise RuntimeError("injected execute denial settlement failure")

    monkeypatch.setattr(budgets, "stage_action_settlement", fail_after_staging)
    executor = AsyncMock()
    with pytest.raises(RuntimeError, match="execute denial settlement failure"):
        await broker.execute_action(
            db_session,
            action_id=action_id,
            claims=claims,
            executor=executor,
            args=args,
        )

    stored = await db_session.get(LabToolAction, action_id)
    budget = await db_session.get(LabRunBudget, "run-execute-denial-rollback")
    assert stored.status == "approved"
    assert stored.result_json is None
    assert budget.reserved_tool_calls == 1
    assert budget.reserved_egress_requests == 1
    executor.assert_not_called()
