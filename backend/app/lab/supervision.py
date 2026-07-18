"""Gateway-side runtime supervision (PRD §Simverse Lab Runtime Protocol v1).

Every REAL runtime adapter (P2-F) reaches the ledger through this seam; the Mock
path does NOT (it has no provider stream). Four responsibilities:

* **Handshake enforcement** — ``open_session`` fails closed on a version /
  capability mismatch BEFORE any ``run.started`` can be written, so an untrusted
  runtime never opens a run (PRD: fail-closed on mismatch).
* **Provider-cursor stream discipline** — the provider ``cursor`` is a
  runtime-side monotonic counter, *distinct* from the ledger's durable ``seq``
  (the caller sources ``seq`` from ``ledger.next_seq``; the cursor is only the
  dedup / ACK key). ``ingest_provider_event`` dedups a replayed cursor to exactly
  one canonical row and applies backpressure; ``ack_through`` advances the ACK
  watermark only to the highest CONTIGUOUS committed cursor (a gap never lets an
  ACK jump past it); ``replay_window`` tells a reconnecting runtime where to
  resume.
* **Cancel escalation** — ``cancel_run`` drives cooperative → TERM → KILL and,
  regardless of which tier the runtime finally acknowledges at, ALWAYS revokes
  the run's grants + bumps the lease fencing epoch (so the orchestrator, still
  at the old epoch, is fenced on its next heartbeat/emit) + writes a terminal
  ``run.failed`` carrying the escalation evidence.
* **Kill-switch drill** — ``kill_switch_all`` cancels every active run, refunds
  its task, revokes its grants and fences it; it is strictly idempotent (a second
  call finds no active run and is a no-op).

Design resolutions (the brief flagged these as open — pinned here + in tests):

* **Backpressure = exception, not a sentinel.** ``ingest_provider_event`` checks
  the unacked window *before* writing; over-limit sets ``session.paused`` and
  raises ``Backpressure`` so the caller stops reading. The event is neither
  written nor dropped — the cursor is not advanced, so the runtime re-sends it
  from ``replay_window`` after an ACK drains the window.
* **ACK is gap-safe and idempotent.** ``ack_through(C)`` advances to the largest
  ``M ≤ C`` such that every cursor in ``(acked, M]`` is committed; a gap stops
  the advance at the gap, below ``C``.
* **The unacked window counts *committed* events only.** A cursor deduped by the
  ledger (already recorded) is not double-counted; ACK only debits cursors this
  session actually counted.
* **Session state is in-memory; the ACK watermark re-derives from the
  ledger.** ``RuntimeSession`` lives only in the supervisor process, so a
  restart loses its in-flight flow-control state (unacked window, pause,
  cancel) — but not durability: ``reopen_session`` recovers
  ``provider_cursor_acked`` via ``rederive_acked_watermark`` (the highest
  COMMITTED ``provider_event_id`` in the ledger, not necessarily what the
  crashed session had actually ACKed back to the runtime — everything at or
  below it is already durable, so handing it back as acked cannot lose data;
  the runtime resumes at ``max + 1`` and the ledger's dedup absorbs any
  resend below that). Every other field resets fresh. ``record_checkpoint`` /
  ``latest_checkpoint`` / ``resume_decision`` give a restarted supervisor a
  ledger-backed resume DECISION (checkpoint present → resume from its ref;
  absent → new attempt). This module owns no new table — both pipelines read
  ``lab_run_events`` that already exists. Honest boundary: actually driving a
  real runtime to continue execution from a checkpoint still needs a real
  adapter (P2-F) through this same seam — deferred, no endpoint exists yet.

Like ``ledger`` / ``leases`` / ``grants``, this module never opens its own
session: the caller owns the transaction boundary. The window thresholds are
read from ``protocol`` at call time so tests can shrink them deterministically.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, UTC

from sqlalchemy import func, select, update

from app.config import settings
from app.lab import grants, leases, ledger, protocol
from app.lab.protocol import HandshakeManifest, RunEventEnvelope
from app.models.lab_event import LabRunEvent
from app.models.lab_grant import LabCapabilityGrant
from app.models.lab_lease import LabRunLease
from app.models.lab_run import LabRun
from app.models.lab_task import LabTask
from app.services import lab_task_service

logger = logging.getLogger(__name__)

_ACTIVE_RUN_STATES = ("queued", "running", "needs_approval")
_TERMINAL_TASK_STATES = ("completed", "cancelled", "failed", "expired")
_CANCEL_POLL_S = 0.1  # how often escalation re-checks runtime liveness within a tier


class SupervisionError(Exception):
    """Base for supervision-layer failures."""


class HandshakeRejected(SupervisionError):
    """A runtime handshake was refused before the run could start (fail-closed)."""


class Backpressure(SupervisionError):
    """The unacked-event window is full — the caller must stop reading the
    provider stream until an ``ack_through`` drains it. The over-limit event was
    neither written nor dropped (the cursor was not advanced)."""

    def __init__(self, unacked_events: int, unacked_bytes: int):
        super().__init__(f"backpressure: {unacked_events} events / {unacked_bytes} bytes unacked")
        self.unacked_events = unacked_events
        self.unacked_bytes = unacked_bytes


@dataclass
class RuntimeSession:
    """In-memory supervision state for one runtime connection.

    ``provider_cursor_acked`` is the highest CONTIGUOUS cursor acknowledged back
    to the runtime; ``unacked_events`` / ``unacked_bytes`` size the flow-control
    window over committed-but-unacked events only.
    """
    run_id: str
    manifest: HandshakeManifest
    provider_cursor_acked: int = 0
    unacked_events: int = 0
    unacked_bytes: int = 0
    paused: bool = False
    cancelled: bool = False
    # committed provider cursors (for contiguity); per-cursor byte sizes of the
    # cursors still counted toward the unacked window (for ACK debiting).
    _committed: set[int] = field(default_factory=set, repr=False)
    _unacked_sizes: dict[int, int] = field(default_factory=dict, repr=False)

    def _highest_contiguous(self, ceiling: int) -> int:
        """The largest ``M ≤ ceiling`` with every cursor in ``(acked, M]``
        committed — a gap stops the walk."""
        c = self.provider_cursor_acked
        while (c + 1) <= ceiling and (c + 1) in self._committed:
            c += 1
        return c


# ── handshake ─────────────────────────────────────────────────────────

async def open_session(db, *, run_id: str, manifest: HandshakeManifest) -> RuntimeSession:
    """Validate the handshake and open a supervision session. Raises
    ``HandshakeRejected`` (fail-closed) on a version / capability mismatch —
    before the caller emits ``run.started``, so a rejected runtime never opens a
    run. ``db`` is accepted for interface symmetry; a rejected handshake writes
    nothing."""
    try:
        protocol.validate_handshake(manifest)
    except protocol.ProtocolError as exc:
        raise HandshakeRejected(str(exc)) from exc
    return RuntimeSession(run_id=run_id, manifest=manifest)


# ── restart recovery: watermark re-derivation ───────────────────────────

async def rederive_acked_watermark(db, *, run_id: str) -> int:
    """The ACK watermark a restarted supervisor recovers for ``run_id``: the
    highest COMMITTED ``provider_event_id`` cursor already durable in the
    ledger — NOT necessarily the cursor the crashed session had actually told
    the runtime was acked (that in-memory fact is gone with the old
    ``RuntimeSession``). Every cursor at or below this value is guaranteed
    already recorded, so handing it back as ``provider_cursor_acked`` cannot
    lose data: the runtime resumes at ``max + 1``, and any resend below that
    is absorbed for free by the ledger's ``provider_event_id`` dedup. Rows
    with a NULL ``provider_event_id`` (non-provider-sourced events, e.g. a
    supervisor-authored ``run.failed``) carry no cursor and are excluded.
    ``provider_event_id`` is stored as a string, so the max is computed in
    Python — a SQL ``MAX`` would sort lexicographically and mis-rank e.g.
    ``"9"`` above ``"10"``."""
    cursors = (
        await db.execute(
            select(LabRunEvent.provider_event_id).where(
                LabRunEvent.run_id == run_id,
                LabRunEvent.provider_event_id.isnot(None),
            )
        )
    ).scalars().all()
    return max((int(c) for c in cursors), default=0)


async def reopen_session(db, *, run_id: str, manifest: HandshakeManifest) -> RuntimeSession:
    """Equivalent to ``open_session`` for a supervisor reconnecting to an
    in-flight run after a restart: the same fail-closed handshake validation,
    but ``provider_cursor_acked`` is recovered via ``rederive_acked_watermark``
    instead of starting at 0. Every other ``RuntimeSession`` field resets
    fresh (unacked window empty, not paused, not cancelled) — those are pure
    in-process flow-control state with no durable counterpart, so a crash
    simply drops them."""
    session = await open_session(db, run_id=run_id, manifest=manifest)
    session.provider_cursor_acked = await rederive_acked_watermark(db, run_id=run_id)
    return session


# ── provider event stream: dedup / backpressure / ACK / replay ─────────

async def ingest_provider_event(db, session: RuntimeSession, *, provider_cursor: int,
                                envelope_builder) -> "ledger.LabRunEvent | None":
    """Ingest one provider event at ``provider_cursor``.

    * ``provider_cursor <= provider_cursor_acked`` → a replay below the ACK
      watermark: dropped as a duplicate, returns ``None``.
    * unacked window full (events or bytes over the ``protocol`` limit) →
      ``session.paused = True`` and ``raise Backpressure``; nothing is written and
      the cursor is not advanced, so the runtime re-sends it after an ACK drains
      the window.
    * otherwise the caller's ``envelope_builder(seq)`` builds the envelope at the
      durable ``seq`` this function sources from ``ledger.next_seq``; the append
      dedups on ``provider_event_id = str(provider_cursor)`` (one canonical row).
    """
    if provider_cursor <= session.provider_cursor_acked:
        return None

    if (session.unacked_events >= protocol.MAX_UNACKED_EVENTS
            or session.unacked_bytes >= protocol.MAX_UNACKED_BYTES):
        session.paused = True
        raise Backpressure(session.unacked_events, session.unacked_bytes)

    seq = await ledger.next_seq(db, session.run_id)
    envelope = envelope_builder(seq)
    event = await ledger.append_event(
        db, envelope=envelope, provider_event_id=str(provider_cursor),
        outbox_topic="lab_run_event",
    )
    session._committed.add(provider_cursor)  # idempotent; committed in the DB either way
    if event is None:
        return None  # ledger dedup: already recorded, do not double-count the window

    size = len(protocol.canonical_json(envelope.payload).encode("utf-8"))
    session._unacked_sizes[provider_cursor] = size
    session.unacked_events += 1
    session.unacked_bytes += size
    return event


async def ack_through(db, session: RuntimeSession, *, provider_cursor: int) -> None:
    """Advance the ACK watermark to the highest CONTIGUOUS committed cursor
    ``≤ provider_cursor`` (a gap blocks the advance below it). Debits the newly
    acknowledged cursors from the unacked window and clears backpressure. ``db``
    is accepted for interface symmetry (the watermark is in-memory)."""
    new_acked = session._highest_contiguous(provider_cursor)
    if new_acked <= session.provider_cursor_acked:
        return  # nothing contiguous to advance (gap, or already acked)

    for c in range(session.provider_cursor_acked + 1, new_acked + 1):
        size = session._unacked_sizes.pop(c, None)
        if size is not None:  # only cursors this session counted are debited
            session.unacked_events = max(0, session.unacked_events - 1)
            session.unacked_bytes = max(0, session.unacked_bytes - size)
    session.provider_cursor_acked = new_acked
    # Prune cursors now below the ACK watermark: contiguity only ever walks
    # forward from ``provider_cursor_acked``, so acked cursors are dead weight —
    # this bounds ``_committed`` instead of letting it grow for the run's life.
    session._committed = {c for c in session._committed if c > new_acked}
    session.paused = False


def replay_window(session: RuntimeSession) -> int:
    """The cursor a reconnecting runtime must resume from (acked + 1)."""
    return session.provider_cursor_acked + 1


# ── checkpoint recording + resume decision ──────────────────────────────

async def record_checkpoint(db, *, run_id: str, seq: int, checkpoint_ref: str,
                            expected_epoch: int | None = None) -> LabRunEvent:
    """Emit a ``checkpoint.created`` event recording where a runtime's durable
    state lives (``checkpoint_ref``), through the same ledger + redaction path
    as any other event — so a later restart's resume decision
    (``latest_checkpoint`` / ``resume_decision``) has something to resume
    from. Gated by ``expected_epoch`` like any ledger write: given, a stale
    epoch (a takeover fenced this writer since it last read one) raises
    ``leases.StaleEpoch`` and writes nothing; omitted, the checkpoint is
    stamped with the run's current epoch and written ungated."""
    run = await db.get(LabRun, run_id)
    task = await db.get(LabTask, run.task_id) if run is not None else None
    tenant_id = task.issuer_user_id if task is not None else (run.researcher_slug if run is not None else "unknown")
    epoch = expected_epoch if expected_epoch is not None else await leases.current_epoch(db, run_id)
    envelope = RunEventEnvelope(
        event_id=str(uuid.uuid4()), tenant_id=tenant_id, run_id=run_id,
        task_id=run.task_id if run is not None else "", seq=seq, type="checkpoint.created",
        actor="supervisor", fencing_epoch=epoch, policy_version=settings.lab_policy_version,
        occurred_at=datetime.now(UTC), payload={"checkpoint_ref": checkpoint_ref},
    )
    return await ledger.append_event(
        db, envelope=envelope, expected_epoch=expected_epoch, outbox_topic="lab_run_event",
    )


def latest_checkpoint(events: list[LabRunEvent]) -> dict | None:
    """The payload of the most recent ``checkpoint.created`` event in
    ``events`` (ledger order), or ``None`` if the run never checkpointed. A
    pure helper over an already-read event list — no ledger I/O — so the
    resume decision it feeds (``resume_decision``) is testable with a fake
    event stream."""
    for event in reversed(events):
        if event.type == "checkpoint.created":
            return event.payload_json
    return None


def resume_decision(events: list[LabRunEvent]) -> dict:
    """Whether a supervisor restart should resume from the run's last
    committed checkpoint or start a fresh attempt, given its event list.
    Honest boundary: this is only the DECISION — a committed checkpoint means
    the runtime should resume from its ``checkpoint_ref`` without replaying
    already-completed side effects; no checkpoint means a new attempt.
    Actually driving a real runtime to continue execution from that ref needs
    a real adapter (P2-F) through this same seam — deferred, no endpoint
    exists yet; this function only classifies the ledger state a caller would
    act on once one does."""
    checkpoint = latest_checkpoint(events)
    if checkpoint is None:
        return {"action": "new_attempt"}
    return {"action": "resume", "checkpoint_ref": checkpoint.get("checkpoint_ref")}


# ── cancel escalation + fencing ───────────────────────────────────────

async def _adapter_maybe(adapter, method: str, handle, *, timeout_s: float):
    """Call an OPTIONAL adapter control method (cancel/terminate/kill) defensively.
    The adapter is UNTRUSTED — cancelling a malicious / faulty runtime is exactly
    when its hook is most likely to raise or hang — so the call is bounded by
    ``timeout_s`` and any error/timeout is swallowed. Fencing must never depend on
    adapter goodwill (the caller lands revoke + epoch bump regardless). A legacy
    adapter lacking the method is a silent no-op."""
    fn = getattr(adapter, method, None)
    if fn is None:
        return None
    try:
        return await asyncio.wait_for(fn(handle), timeout=timeout_s)
    except Exception:  # noqa: BLE001 — untrusted hook: timeout or any error → drop it
        logger.warning("cancel escalation adapter.%s faulted/timed out", method, exc_info=True)
        return None


async def _runtime_stopped(adapter, handle, *, timeout_s: float) -> bool:
    """Whether the runtime has stopped / acknowledged cancel. An adapter without
    ``health`` (Mock / legacy: no live process) reports stopped, so a cooperative
    cancel resolves immediately. A health probe that hangs or throws is bounded by
    ``timeout_s`` and read as 'not stopped' — unknown liveness fails toward further
    escalation + fence, never a block."""
    fn = getattr(adapter, "health", None)
    if fn is None:
        return True
    try:
        h = (await asyncio.wait_for(fn(handle), timeout=timeout_s)) or {}
    except Exception:  # noqa: BLE001 — unknown liveness → escalate, never wait forever
        return False
    return bool(h.get("cancelled")) or not bool(h.get("alive", False))


async def _wait_stopped(adapter, handle, *, deadline: float, now, sleep, timeout_s: float) -> bool:
    """Poll runtime liveness until it stops or ``deadline`` (injected clock) — no
    real waits in tests, where ``sleep`` advances the clock."""
    while True:
        if await _runtime_stopped(adapter, handle, timeout_s=timeout_s):
            return True
        if now() >= deadline:
            return False
        await sleep(_CANCEL_POLL_S)


async def _escalate_cancel(adapter, handle, *, grace_s: float, kill_s: float,
                           now, sleep, timeout_s: float) -> str:
    """cooperative (≤grace_s) → TERM (≤kill_s total) → KILL. Returns the tier the
    runtime finally acknowledged at. Every adapter call is timeout-bounded and
    fault-swallowing (``_adapter_maybe`` / ``_runtime_stopped``), so this never
    raises from adapter behaviour and never blocks forever."""
    t0 = now()
    await _adapter_maybe(adapter, "cancel", handle, timeout_s=timeout_s)
    if await _wait_stopped(adapter, handle, deadline=t0 + grace_s, now=now, sleep=sleep, timeout_s=timeout_s):
        return "cooperative"
    await _adapter_maybe(adapter, "terminate", handle, timeout_s=timeout_s)
    if await _wait_stopped(adapter, handle, deadline=t0 + kill_s, now=now, sleep=sleep, timeout_s=timeout_s):
        return "term"
    await _adapter_maybe(adapter, "kill", handle, timeout_s=timeout_s)
    return "kill"


async def _bump_epoch(db, run_id: str) -> int:
    """Structurally fence the current owner: a direct ``fencing_epoch += 1`` on
    the lease (takeover semantics). The old owner's next heartbeat/emit carries
    the stale epoch → rejected. No lease row (never started) → epoch stays 0."""
    await db.execute(
        update(LabRunLease)
        .where(LabRunLease.run_id == run_id)
        .values(fencing_epoch=LabRunLease.fencing_epoch + 1)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return await leases.current_epoch(db, run_id)


async def _emit_terminal(db, run_id: str, *, reason: str, extra: dict | None = None) -> None:
    """Append a supervisor-authored ``run.failed`` for the run. No fencing gate
    (the supervisor is an out-of-band authority, not the lease holder); the
    envelope carries the run's current epoch."""
    run = await db.get(LabRun, run_id)
    if run is None:
        return
    task = await db.get(LabTask, run.task_id)
    tenant_id = task.issuer_user_id if task is not None else run.researcher_slug
    epoch = await leases.current_epoch(db, run_id)
    seq = await ledger.next_seq(db, run_id)
    payload = {"reason": reason}
    if extra:
        payload.update(extra)
    envelope = RunEventEnvelope(
        event_id=str(uuid.uuid4()), tenant_id=tenant_id, run_id=run_id, task_id=run.task_id,
        seq=seq, type="run.failed", actor="supervisor", fencing_epoch=epoch,
        policy_version=settings.lab_policy_version, occurred_at=datetime.now(UTC),
        payload=payload,
    )
    await ledger.append_event(db, envelope=envelope, outbox_topic="lab_run_event")


async def cancel_run(db, *, run_id: str, adapter, handle, reason: str,
                     grace_s: float = protocol.CANCEL_GRACE_S,
                     kill_s: float = protocol.CANCEL_KILL_S,
                     control_timeout_s: float = protocol.CANCEL_GRACE_S,
                     now=None, sleep=asyncio.sleep) -> str:
    """Cooperatively cancel a run, escalating to TERM then KILL. Regardless of
    which tier the runtime acknowledges at — and regardless of any adapter fault —
    this ALWAYS revokes the run's grants, bumps the lease fencing epoch (fencing
    the orchestrator), flips the run to ``cancelled``, and writes a terminal
    ``run.failed`` with the escalation evidence.

    The fence lives in a ``finally`` and is unconditional: the adapter is
    untrusted, so a ``cancel`` / ``terminate`` / ``kill`` / ``health`` hook that
    raises or hangs (bounded by ``control_timeout_s``, swallowed) can never let a
    malicious/faulty runtime dodge grant revocation + epoch bump. Returns
    ``"cooperative" | "term" | "kill"``. ``now`` / ``sleep`` / ``control_timeout_s``
    are injectable so escalation windows and hung-hook timeouts are instant in
    tests."""
    clock = now if now is not None else time.monotonic
    tier = "kill"  # worst-case default if escalation itself somehow raises
    try:
        tier = await _escalate_cancel(adapter, handle, grace_s=grace_s, kill_s=kill_s,
                                      now=clock, sleep=sleep, timeout_s=control_timeout_s)
    finally:
        # Fencing must land under ANY escalation outcome or exception.
        await grants.revoke_run_grants(db, run_id)
        await _bump_epoch(db, run_id)
        run = await db.get(LabRun, run_id)
        if run is not None and run.status not in ("succeeded", "failed", "cancelled"):
            run.status = "cancelled"
            run.ended_at = datetime.now(UTC)
            await db.commit()
        await _emit_terminal(db, run_id, reason=f"cancelled:{reason}", extra={"escalation": tier})
    return tier


# ── kill-switch drill ─────────────────────────────────────────────────

async def kill_switch_all(db, *, now: datetime | None = None) -> dict:
    """Terminate every active run (``queued`` / ``running`` / ``needs_approval``):
    flip it to ``cancelled``, revoke its grants, fence its lease, refund the task,
    and write a ``run.failed(reason="kill_switch")``. Strictly idempotent — a
    second call finds no active run and is a no-op. Per-run failures are isolated
    (logged + rolled back) so one bad run cannot abort the drill.

    Follows ``expire_lab_tasks``'s single-session, commit-per-item idiom (the
    brief's ``db_factory`` sketch is superseded: a passed session is testable both
    directly and from the admin route's request scope, and keeps this module's
    "never opens its own session" contract)."""
    now = now if now is not None else datetime.now(UTC)
    stats = {"runs_cancelled": 0, "grants_revoked": 0, "tasks_failed": 0}

    active = (await db.execute(
        select(LabRun).where(LabRun.status.in_(_ACTIVE_RUN_STATES))
    )).scalars().all()

    for run in active:
        run_id = run.id
        try:
            live_grants = (await db.execute(
                select(func.count()).select_from(LabCapabilityGrant).where(
                    LabCapabilityGrant.run_id == run_id,
                    LabCapabilityGrant.revoked_at.is_(None),
                )
            )).scalar_one()

            # Refund the escrow FIRST (and let ``fail_task`` commit it) BEFORE
            # flipping the run to a terminal state. If the refund raises, the run
            # stays active and this drill (or the next) retries it — committing
            # ``run=cancelled`` ahead of a guaranteed refund would strand the hold
            # forever, since a cancelled run is no longer in _ACTIVE_RUN_STATES to
            # be re-swept. ``fail_task``'s task-terminal guard makes the retry
            # idempotent (never a double refund).
            task = await db.get(LabTask, run.task_id)
            refunded = False
            if task is not None and task.status not in _TERMINAL_TASK_STATES:
                await lab_task_service.fail_task(db, task, reason="kill_switch")
                refunded = True

            await grants.revoke_run_grants(db, run_id)
            await _bump_epoch(db, run_id)

            run.status = "cancelled"
            run.ended_at = now
            await db.commit()
            await _emit_terminal(db, run_id, reason="kill_switch")

            stats["runs_cancelled"] += 1
            stats["grants_revoked"] += int(live_grants)
            if refunded:
                stats["tasks_failed"] += 1
        except Exception:  # noqa: BLE001 — isolate one bad run; the drill goes on
            logger.warning("kill_switch teardown failed for run %s", run_id, exc_info=True)
            await db.rollback()

    return stats
