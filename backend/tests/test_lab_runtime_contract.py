"""P2-D — Gateway runtime supervision layer contract (PRD §Simverse Lab Runtime
Protocol v1: handshake / provider-cursor ACK / backpressure / cancel escalation,
plus the kill-switch drill). V04 / V05 / V06 + kill switch.

The supervision layer is the seam any REAL runtime adapter (P2-F) must pass
through: it enforces the handshake before a run can start, dedups + backpressures
the provider event stream (the provider ``cursor`` is a runtime-side counter,
distinct from the ledger's durable ``seq``), acknowledges only up to the highest
CONTIGUOUS committed cursor (a gap must not let an ACK jump past it), and
escalates a cooperative cancel to TERM then KILL while ALWAYS revoking the run's
grants and bumping the lease fencing epoch. The Mock path never flows through
here (Mock has no provider stream), so these tests drive supervision directly
with a deterministic in-test fake runtime + an injected clock — no real waits.

Cross-session note (mirrors test_lab_task_flow / test_lab_e2e): each step opens
its own ``async_session`` on the shared in-memory engine; supervision commits at
every mutation so a fresh session always sees the prior one's writes.
"""
import asyncio
import uuid
from datetime import datetime, UTC

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.lab import grants, leases, ledger, protocol, supervision
from app.lab.protocol import HandshakeManifest, RunEventEnvelope
from app.models.lab_action import LabToolAction
from app.models.lab_event import LabRunEvent
from app.models.lab_grant import LabCapabilityGrant
from app.models.lab_lease import LabRunLease
from app.models.lab_run import LabRun
from app.models.lab_task import LabTask
from app.models.user import User
from app.services.auth_service import create_token


# ── fixtures / helpers ────────────────────────────────────────────────

@pytest.fixture
def sup_env(db_engine, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "lab_grant_secret", "test-secret", raising=False)
    monkeypatch.setattr(settings, "lab_agent_v1_enabled", True, raising=False)
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


def _good_manifest(**over) -> HandshakeManifest:
    base = dict(protocol_version=1, runtime="fake", runtime_version="1",
                capabilities=["broker_mediation"])
    base.update(over)
    return HandshakeManifest(**base)


def _builder(cursor: int, *, run_id="run1", tenant_id="t1", task_id="task1", body="x"):
    """A provider-event envelope factory the supervisor stamps a durable seq onto."""
    def build(seq: int) -> RunEventEnvelope:
        return RunEventEnvelope(
            event_id=str(uuid.uuid4()), tenant_id=tenant_id, run_id=run_id, task_id=task_id,
            seq=seq, type="plan.updated", actor="runtime", fencing_epoch=0,
            policy_version="lab-policy-v1", occurred_at=datetime.now(UTC),
            payload={"cursor": cursor, "summary": f"event {cursor} {body}"},
        )
    return build


async def _ingest(db, session, cursor):
    return await supervision.ingest_provider_event(
        db, session, provider_cursor=cursor, envelope_builder=_builder(cursor))


async def _event_count(db, run_id="run1", type_="plan.updated"):
    return (await db.execute(
        select(func.count()).select_from(LabRunEvent)
        .where(LabRunEvent.run_id == run_id, LabRunEvent.type == type_)
    )).scalar_one()


class FakeClock:
    """Injected time source: ``sleep`` advances the clock, so escalation windows
    elapse instantly and deterministically (no real waits)."""
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    async def sleep(self, seconds):
        self.t += seconds


class FakeRuntimeAdapter:
    """A programmable runtime: ``stops_at`` selects which escalation tier it
    finally acknowledges cancel at ('cancel' | 'terminate' | 'kill')."""
    name = "fake"

    def __init__(self, *, stops_at):
        self.stops_at = stops_at
        self._stopped = False
        self.calls: list[str] = []

    async def cancel(self, handle):
        self.calls.append("cancel")
        if self.stops_at == "cancel":
            self._stopped = True

    async def terminate(self, handle):
        self.calls.append("terminate")
        if self.stops_at == "terminate":
            self._stopped = True

    async def kill(self, handle):
        self.calls.append("kill")
        self._stopped = True  # KILL always stops

    async def health(self, handle):
        return {"alive": not self._stopped, "cancelled": self._stopped}


class FakeRaisingAdapter:
    """An untrusted runtime whose every cancel hook (and health) RAISES — the
    supervisor must fence anyway."""
    name = "boom"

    def __init__(self):
        self.calls: list[str] = []

    async def cancel(self, handle):
        self.calls.append("cancel")
        raise RuntimeError("cancel boom")

    async def terminate(self, handle):
        self.calls.append("terminate")
        raise RuntimeError("terminate boom")

    async def kill(self, handle):
        self.calls.append("kill")
        raise RuntimeError("kill boom")

    async def health(self, handle):
        raise RuntimeError("health boom")


class FakeHangingAdapter:
    """An untrusted runtime whose cancel hooks HANG forever; only the injected
    control timeout unblocks the supervisor, which must still fence."""
    name = "hang"

    def __init__(self):
        self.calls: list[str] = []

    async def cancel(self, handle):
        self.calls.append("cancel")
        await asyncio.sleep(3600)

    async def terminate(self, handle):
        self.calls.append("terminate")
        await asyncio.sleep(3600)

    async def kill(self, handle):
        self.calls.append("kill")
        await asyncio.sleep(3600)

    async def health(self, handle):
        return {"alive": True, "cancelled": False}  # never reports stopped


async def _seed_run(factory, *, run_id="run1", task_id="task1", status="running",
                    issuer="issuer", hold_id=None, epoch0_owner="owner-A", with_grant=True):
    async with factory() as s:
        s.add(LabTask(id=task_id, issuer_user_id=issuer, title="t", hold_id=hold_id,
                      status="running"))
        s.add(LabRun(id=run_id, task_id=task_id, researcher_slug="sage",
                     status=status, adapter="mock"))
        await s.commit()
        if epoch0_owner:
            await leases.acquire_lease(s, run_id=run_id, owner_id=epoch0_owner)
        jti = None
        if with_grant:
            _, claims = await grants.issue_run_grant(
                s, tenant_id=issuer, task_id=task_id, run_id=run_id,
                agent_id="a", capabilities=["web_search"], fencing_epoch=0,
            )
            jti = claims.jti
        return jti


# ── V04: handshake enforcement (before any run.started) ────────────────

@pytest.mark.anyio
async def test_v04_bad_protocol_version_rejected_before_run_started(sup_env):
    factory = sup_env
    async with factory() as db:
        with pytest.raises(supervision.HandshakeRejected):
            await supervision.open_session(db, run_id="run1", manifest=_good_manifest(protocol_version=2))
        # Handshake failed => no run.started (indeed no event at all) was written.
        assert await _event_count(db, type_="run.started") == 0
        assert (await db.execute(
            select(func.count()).select_from(LabRunEvent).where(LabRunEvent.run_id == "run1")
        )).scalar_one() == 0


@pytest.mark.anyio
async def test_v04_missing_broker_mediation_rejected(sup_env):
    factory = sup_env
    async with factory() as db:
        with pytest.raises(supervision.HandshakeRejected):
            await supervision.open_session(db, run_id="run1", manifest=_good_manifest(capabilities=["fs"]))


@pytest.mark.anyio
async def test_v04_valid_manifest_opens_session(sup_env):
    factory = sup_env
    async with factory() as db:
        session = await supervision.open_session(db, run_id="run1", manifest=_good_manifest())
        assert session.run_id == "run1"
        assert session.provider_cursor_acked == 0
        assert session.paused is False and session.cancelled is False


# ── V05: cursor dedup / gap-safe ACK / replay / backpressure ───────────

@pytest.mark.anyio
async def test_v05_duplicate_cursor_writes_one_canonical_row(sup_env):
    factory = sup_env
    async with factory() as db:
        session = await supervision.open_session(db, run_id="run1", manifest=_good_manifest())
        first = await _ingest(db, session, 5)
        dup = await _ingest(db, session, 5)
        assert first is not None
        assert dup is None                         # deduped, not written
        assert await _event_count(db) == 1         # exactly one canonical row
        assert session.unacked_events == 1         # dup did not double-count


@pytest.mark.anyio
async def test_v05_ack_stops_at_highest_contiguous_cursor(sup_env):
    factory = sup_env
    async with factory() as db:
        session = await supervision.open_session(db, run_id="run1", manifest=_good_manifest())
        await _ingest(db, session, 1)
        await _ingest(db, session, 3)              # gap at 2 (out-of-order arrival)
        assert await _event_count(db) == 2         # both land in the ledger

        await supervision.ack_through(db, session, provider_cursor=3)
        assert session.provider_cursor_acked == 1  # ACK cannot jump past the gap

        await _ingest(db, session, 2)              # gap filled
        await supervision.ack_through(db, session, provider_cursor=3)
        assert session.provider_cursor_acked == 3  # now contiguous → advances fully


@pytest.mark.anyio
async def test_v05_replay_window_is_acked_plus_one(sup_env):
    factory = sup_env
    async with factory() as db:
        session = await supervision.open_session(db, run_id="run1", manifest=_good_manifest())
        for c in (1, 2, 3):
            await _ingest(db, session, c)
        await supervision.ack_through(db, session, provider_cursor=3)
        assert session.provider_cursor_acked == 3
        assert supervision.replay_window(session) == 4   # reconnect resumes at N+1


@pytest.mark.anyio
async def test_v05_backpressure_pauses_without_dropping(sup_env, monkeypatch):
    monkeypatch.setattr(protocol, "MAX_UNACKED_EVENTS", 2)
    factory = sup_env
    async with factory() as db:
        session = await supervision.open_session(db, run_id="run1", manifest=_good_manifest())
        await _ingest(db, session, 1)
        await _ingest(db, session, 2)              # window full (2/2)
        with pytest.raises(supervision.Backpressure):
            await _ingest(db, session, 3)          # over the window → pause
        assert session.paused is True
        assert await _event_count(db) == 2         # event 3 NOT dropped-to-ledger

        # ACK drains the window → resume; the paused event now ingests.
        await supervision.ack_through(db, session, provider_cursor=2)
        assert session.paused is False and session.unacked_events == 0
        assert await _ingest(db, session, 3) is not None
        assert await _event_count(db) == 3


# ── V06: cooperative-cancel → TERM → KILL escalation + fencing ─────────

@pytest.mark.anyio
async def test_v06_cooperative_cancel_revokes_and_fences(sup_env):
    factory = sup_env
    jti = await _seed_run(factory)
    adapter = FakeRuntimeAdapter(stops_at="cancel")
    clock = FakeClock()

    async with factory() as db:
        tier = await supervision.cancel_run(
            db, run_id="run1", adapter=adapter, handle=None, reason="admin",
            grace_s=0.2, kill_s=0.4, now=clock.now, sleep=clock.sleep,
        )
    assert tier == "cooperative"
    assert adapter.calls == ["cancel"]             # never escalated

    async with factory() as db:
        assert (await db.get(LabCapabilityGrant, jti)).revoked_at is not None
        assert (await db.get(LabRunLease, "run1")).fencing_epoch == 1
        run = await db.get(LabRun, "run1")
        assert run.status == "cancelled" and run.ended_at is not None
        evs = (await db.execute(
            select(LabRunEvent).where(LabRunEvent.run_id == "run1", LabRunEvent.type == "run.failed")
        )).scalars().all()
        assert len(evs) == 1
        assert evs[0].payload_json["reason"].startswith("cancelled:")
        assert evs[0].payload_json["escalation"] == "cooperative"


@pytest.mark.anyio
async def test_v06_refused_cancel_escalates_to_kill_still_fences(sup_env):
    factory = sup_env
    jti = await _seed_run(factory)
    adapter = FakeRuntimeAdapter(stops_at="kill")   # ignores cancel + terminate
    clock = FakeClock()

    async with factory() as db:
        tier = await supervision.cancel_run(
            db, run_id="run1", adapter=adapter, handle=None, reason="runaway",
            grace_s=0.2, kill_s=0.4, now=clock.now, sleep=clock.sleep,
        )
    assert tier == "kill"
    assert adapter.calls == ["cancel", "terminate", "kill"]  # full escalation

    async with factory() as db:
        # Revocation + fencing happen regardless of which tier fired.
        assert (await db.get(LabCapabilityGrant, jti)).revoked_at is not None
        assert (await db.get(LabRunLease, "run1")).fencing_epoch == 1


@pytest.mark.anyio
async def test_v06_term_tier_when_terminate_acks(sup_env):
    factory = sup_env
    await _seed_run(factory)
    adapter = FakeRuntimeAdapter(stops_at="terminate")
    clock = FakeClock()
    async with factory() as db:
        tier = await supervision.cancel_run(
            db, run_id="run1", adapter=adapter, handle=None, reason="x",
            grace_s=0.2, kill_s=0.4, now=clock.now, sleep=clock.sleep,
        )
    assert tier == "term"
    assert adapter.calls == ["cancel", "terminate"]  # stopped before KILL


@pytest.mark.anyio
async def test_v06_cancel_does_not_replay_completed_actions(sup_env):
    factory = sup_env
    await _seed_run(factory)
    adapter = FakeRuntimeAdapter(stops_at="cancel")
    clock = FakeClock()
    async with factory() as db:
        before = (await db.execute(
            select(func.count()).select_from(LabToolAction).where(LabToolAction.run_id == "run1")
        )).scalar_one()
        await supervision.cancel_run(
            db, run_id="run1", adapter=adapter, handle=None, reason="x",
            grace_s=0.2, kill_s=0.4, now=clock.now, sleep=clock.sleep,
        )
    async with factory() as db:
        after = (await db.execute(
            select(func.count()).select_from(LabToolAction).where(LabToolAction.run_id == "run1")
        )).scalar_one()
    assert before == 0 and after == 0              # cancel executes/replays no actions


@pytest.mark.anyio
async def test_v06_cancel_fences_even_when_adapter_raises(sup_env):
    """The adapter is untrusted: every cancel/terminate/kill/health hook raises.
    Fencing (grant revocation + epoch bump + terminal run.failed) must land anyway
    — a faulty runtime cannot dodge the fence."""
    factory = sup_env
    jti = await _seed_run(factory)
    adapter = FakeRaisingAdapter()
    clock = FakeClock()

    async with factory() as db:
        tier = await supervision.cancel_run(
            db, run_id="run1", adapter=adapter, handle=None, reason="fault",
            grace_s=0.2, kill_s=0.4, control_timeout_s=0.05, now=clock.now, sleep=clock.sleep,
        )
    assert tier == "kill"                           # health raises → never 'stopped'

    async with factory() as db:
        assert (await db.get(LabCapabilityGrant, jti)).revoked_at is not None
        assert (await db.get(LabRunLease, "run1")).fencing_epoch == 1
        run = await db.get(LabRun, "run1")
        assert run.status == "cancelled"
        evs = (await db.execute(
            select(LabRunEvent).where(LabRunEvent.run_id == "run1", LabRunEvent.type == "run.failed")
        )).scalars().all()
        assert len(evs) == 1


@pytest.mark.anyio
async def test_v06_cancel_fences_even_when_adapter_hangs(sup_env):
    """The adapter hangs forever on every cancel hook. The injected control
    timeout unblocks each tier; fencing must still land (never a deadlock)."""
    factory = sup_env
    jti = await _seed_run(factory)
    adapter = FakeHangingAdapter()
    clock = FakeClock()

    async with factory() as db:
        tier = await supervision.cancel_run(
            db, run_id="run1", adapter=adapter, handle=None, reason="hang",
            grace_s=0.2, kill_s=0.4, control_timeout_s=0.02, now=clock.now, sleep=clock.sleep,
        )
    assert tier == "kill"
    assert adapter.calls == ["cancel", "terminate", "kill"]  # each timed out, escalated

    async with factory() as db:
        assert (await db.get(LabCapabilityGrant, jti)).revoked_at is not None
        assert (await db.get(LabRunLease, "run1")).fencing_epoch == 1
        assert (await db.get(LabRun, "run1")).status == "cancelled"


# ── A2: supervisor watermark re-derivation + checkpoint records ────────

def _fake_event(*, type_: str, payload: dict, run_id: str = "run1", seq: int = 1) -> LabRunEvent:
    """An unpersisted LabRunEvent row — enough surface for the pure decision
    helpers (latest_checkpoint / resume_decision) without touching the ledger."""
    return LabRunEvent(
        event_id=str(uuid.uuid4()), tenant_id="t1", run_id=run_id, task_id="task1",
        seq=seq, type=type_, actor="runtime", fencing_epoch=0,
        policy_version="lab-policy-v1", payload_json=payload, occurred_at=datetime.now(UTC),
    )


@pytest.mark.anyio
async def test_rederive_watermark_recovers_committed_max_not_pre_restart_ack(sup_env):
    """The crashed session had only acked up to 2 (a gap-safe partial ACK), but
    all three events are already durably committed to the ledger — rederive
    must return the committed max (3), not the stale in-memory ack (2), since
    everything at or below 3 is safely already recorded."""
    factory = sup_env
    async with factory() as db:
        session = await supervision.open_session(db, run_id="run1", manifest=_good_manifest())
        for c in (1, 2, 3):
            await _ingest(db, session, c)
        await supervision.ack_through(db, session, provider_cursor=2)
        assert session.provider_cursor_acked == 2  # pre-restart state, for contrast

    async with factory() as db:
        assert await supervision.rederive_acked_watermark(db, run_id="run1") == 3

    async with factory() as db:
        reopened = await supervision.reopen_session(db, run_id="run1", manifest=_good_manifest())
        assert reopened.provider_cursor_acked == 3
        # Every other field resets fresh — no durable counterpart.
        assert reopened.unacked_events == 0
        assert reopened.unacked_bytes == 0
        assert reopened.paused is False
        assert reopened.cancelled is False


@pytest.mark.anyio
async def test_rederive_watermark_zero_when_no_events(sup_env):
    factory = sup_env
    async with factory() as db:
        assert await supervision.rederive_acked_watermark(db, run_id="run-nonexistent") == 0


@pytest.mark.anyio
async def test_rederive_watermark_ignores_null_provider_event_id(sup_env):
    """An event with no provider_event_id (e.g. a supervisor-authored run.failed)
    carries no cursor and must not be folded into the max."""
    factory = sup_env
    async with factory() as db:
        envelope = RunEventEnvelope(
            event_id=str(uuid.uuid4()), tenant_id="t1", run_id="run1", task_id="task1",
            seq=1, type="run.started", actor="runtime", fencing_epoch=0,
            policy_version="lab-policy-v1", occurred_at=datetime.now(UTC), payload={},
        )
        await ledger.append_event(db, envelope=envelope, outbox_topic="lab_run_event")
        assert await supervision.rederive_acked_watermark(db, run_id="run1") == 0


@pytest.mark.anyio
async def test_record_checkpoint_writes_event_with_redacted_payload(sup_env):
    factory = sup_env
    await _seed_run(factory, with_grant=False)
    async with factory() as db:
        event = await supervision.record_checkpoint(
            db, run_id="run1", seq=1,
            checkpoint_ref="s3://bucket/run1/ckpt-1?token=abcdefghijklmnop",
        )
    assert event.type == "checkpoint.created"
    ref = event.payload_json["checkpoint_ref"]
    assert "ckpt-1" in ref
    assert "abcdefghijklmnop" not in ref  # secret-looking token redacted


@pytest.mark.anyio
async def test_record_checkpoint_stale_epoch_writes_nothing(sup_env):
    factory = sup_env
    await _seed_run(factory, with_grant=False)  # acquires the lease at epoch 0
    async with factory() as db:
        with pytest.raises(leases.StaleEpoch):
            await supervision.record_checkpoint(
                db, run_id="run1", seq=1, checkpoint_ref="ckpt-x", expected_epoch=5,
            )
    async with factory() as db:
        assert await _event_count(db, type_="checkpoint.created") == 0


def test_latest_checkpoint_returns_payload_of_last_of_several():
    events = [
        _fake_event(type_="plan.updated", payload={}),
        _fake_event(type_="checkpoint.created", payload={"checkpoint_ref": "ckpt-1"}),
        _fake_event(type_="tool.completed", payload={}),
        _fake_event(type_="checkpoint.created", payload={"checkpoint_ref": "ckpt-2"}),
    ]
    assert supervision.latest_checkpoint(events) == {"checkpoint_ref": "ckpt-2"}


def test_latest_checkpoint_none_when_absent():
    events = [_fake_event(type_="plan.updated", payload={})]
    assert supervision.latest_checkpoint(events) is None


def test_resume_decision_resumes_from_checkpoint_when_present():
    events = [_fake_event(type_="checkpoint.created", payload={"checkpoint_ref": "ckpt-9"})]
    assert supervision.resume_decision(events) == {"action": "resume", "checkpoint_ref": "ckpt-9"}


def test_resume_decision_new_attempt_when_no_checkpoint():
    assert supervision.resume_decision([]) == {"action": "new_attempt"}


# ── kill switch drill ─────────────────────────────────────────────────

@pytest.mark.anyio
async def test_kill_switch_all_cancels_refunds_revokes_idempotently(sup_env):
    factory = sup_env
    # Two active runs under two tasks, each with a live grant + lease + escrow hold.
    async with factory() as s:
        s.add(User(id="issuer", name="I", email="i@t.com", soul_coin_balance=100))
        await s.commit()
    jtis = []
    for i in (1, 2):
        async with factory() as s:
            task = LabTask(id=f"task{i}", issuer_user_id="issuer", title="t", reward_sc=10,
                           platform_fee_sc=0, status="running")
            s.add(task)
            await s.flush()
            from app.services import coin_service
            hold = await coin_service.hold(s, "issuer", 10, f"lab_task:task{i}")
            task.hold_id = hold
            s.add(LabRun(id=f"run{i}", task_id=f"task{i}", researcher_slug="sage",
                         status="running" if i == 1 else "needs_approval", adapter="mock"))
            await s.commit()
            await leases.acquire_lease(s, run_id=f"run{i}", owner_id=f"owner-{i}")
            _, claims = await grants.issue_run_grant(
                s, tenant_id="issuer", task_id=f"task{i}", run_id=f"run{i}",
                agent_id="a", capabilities=["web_search"], fencing_epoch=0,
            )
            jtis.append(claims.jti)

    async with factory() as db:
        stats = await supervision.kill_switch_all(db)
    assert stats["runs_cancelled"] == 2

    async with factory() as db:
        for i in (1, 2):
            assert (await db.get(LabRun, f"run{i}")).status == "cancelled"
            assert (await db.get(LabTask, f"task{i}")).status == "failed"
            assert (await db.get(LabRunLease, f"run{i}")).fencing_epoch == 1
            evs = (await db.execute(
                select(LabRunEvent).where(LabRunEvent.run_id == f"run{i}", LabRunEvent.type == "run.failed")
            )).scalars().all()
            assert len(evs) == 1 and evs[0].payload_json["reason"] == "kill_switch"
        for jti in jtis:
            assert (await db.get(LabCapabilityGrant, jti)).revoked_at is not None
        from app.services import coin_service
        assert await coin_service.get_balance(db, "issuer") == 100  # both holds refunded

    # Idempotent: a second drill touches nothing.
    async with factory() as db:
        stats2 = await supervision.kill_switch_all(db)
    assert stats2["runs_cancelled"] == 0 and stats2["tasks_failed"] == 0
    async with factory() as db:
        for i in (1, 2):
            # Still exactly one terminal event per run (no second run.failed).
            assert (await db.execute(
                select(func.count()).select_from(LabRunEvent)
                .where(LabRunEvent.run_id == f"run{i}", LabRunEvent.type == "run.failed")
            )).scalar_one() == 1


@pytest.mark.anyio
async def test_kill_switch_refund_retried_after_fail_task_error(sup_env, monkeypatch):
    """Escrow-leak guard: if the refund raises, the run must NOT be committed as
    cancelled (else it drops out of the active set and the hold is stranded). A
    later drill retries and the issuer is made whole."""
    from app.services import coin_service
    from app.services import lab_task_service as _svc

    factory = sup_env
    async with factory() as s:
        s.add(User(id="issuer", name="I", email="i@t.com", soul_coin_balance=1000))
        await s.commit()
    async with factory() as s:
        task = LabTask(id="task1", issuer_user_id="issuer", title="t", reward_sc=10,
                       platform_fee_sc=0, status="running")
        s.add(task)
        await s.flush()
        task.hold_id = await coin_service.hold(s, "issuer", 10, "lab_task:task1")
        s.add(LabRun(id="run1", task_id="task1", researcher_slug="sage",
                     status="running", adapter="mock"))
        await s.commit()
        await leases.acquire_lease(s, run_id="run1", owner_id="o")
    async with factory() as s:
        assert await coin_service.get_balance(s, "issuer") == 990  # 10 escrowed

    # fail_task raises on its FIRST invocation only, then behaves normally.
    orig_fail = _svc.fail_task
    calls = {"n": 0}

    async def flaky_fail(db, task, reason=""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("refund boom")
        return await orig_fail(db, task, reason=reason)

    monkeypatch.setattr(_svc, "fail_task", flaky_fail)

    # First drill: refund throws → run stays active, hold NOT refunded (no leak).
    async with factory() as db:
        stats1 = await supervision.kill_switch_all(db)
    assert stats1["runs_cancelled"] == 0
    async with factory() as s:
        assert (await s.get(LabRun, "run1")).status == "running"     # not stranded
        assert await coin_service.get_balance(s, "issuer") == 990    # still escrowed

    # Second drill: refund succeeds → escrow returned, run terminal.
    async with factory() as db:
        stats2 = await supervision.kill_switch_all(db)
    assert stats2["runs_cancelled"] == 1
    async with factory() as s:
        assert (await s.get(LabRun, "run1")).status == "cancelled"
        assert (await s.get(LabTask, "task1")).status == "failed"
        assert await coin_service.get_balance(s, "issuer") == 1000   # fully refunded


# ── admin REST kill-switch terminates active runs ─────────────────────

@pytest.mark.anyio
async def test_admin_rest_kill_switch_terminates_active_runs(client, db_session):
    db_session.add(User(id="admin-k", name="Admin", email="admin-k@t.com", is_admin=True))
    db_session.add(LabTask(id="taskK", issuer_user_id="admin-k", title="t", status="running"))
    db_session.add(LabRun(id="runK", task_id="taskK", researcher_slug="sage",
                          status="running", adapter="mock"))
    await db_session.commit()
    await leases.acquire_lease(db_session, run_id="runK", owner_id="owner-K")
    await db_session.commit()

    # The kill-switch drill runs unconditionally (safety mechanism), independent
    # of the v1 flag — so this test sets no global flag to leak into later tests.
    headers = {"Authorization": f"Bearer {create_token('admin-k')}"}
    resp = await client.post("/admin/lab/kill-switch", json={"enabled": False}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["runtime_enabled"] is False
    assert resp.json()["killed"]["runs_cancelled"] >= 1

    fresh = await db_session.get(LabRun, "runK")
    await db_session.refresh(fresh)
    assert fresh.status == "cancelled"
