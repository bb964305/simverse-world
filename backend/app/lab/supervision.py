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
* **Session state is in-memory.** A supervisor restart re-derives the ACK
  watermark from the ledger via ``replay_window`` semantics on reconnect — this
  module owns no new table.

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
    session.paused = False


def replay_window(session: RuntimeSession) -> int:
    """The cursor a reconnecting runtime must resume from (acked + 1)."""
    return session.provider_cursor_acked + 1


# ── cancel escalation + fencing ───────────────────────────────────────

async def _adapter_maybe(adapter, method: str, handle):
    """Call an OPTIONAL adapter method (cancel/terminate/kill); a legacy adapter
    lacking it is a no-op, so wiring a real adapter through here never breaks the
    Mock / legacy adapters that don't implement the escalation surface."""
    fn = getattr(adapter, method, None)
    if fn is None:
        return None
    return await fn(handle)


async def _runtime_stopped(adapter, handle) -> bool:
    """Whether the runtime has stopped / acknowledged cancel. An adapter without
    ``health`` (Mock / legacy: no live process to wait on) reports stopped, so a
    cooperative cancel resolves immediately."""
    fn = getattr(adapter, "health", None)
    if fn is None:
        return True
    h = (await fn(handle)) or {}
    return bool(h.get("cancelled")) or not bool(h.get("alive", False))


async def _wait_stopped(adapter, handle, *, deadline: float, now, sleep) -> bool:
    """Poll runtime liveness until it stops or ``deadline`` (injected clock) — no
    real waits in tests, where ``sleep`` advances the clock."""
    while True:
        if await _runtime_stopped(adapter, handle):
            return True
        if now() >= deadline:
            return False
        await sleep(_CANCEL_POLL_S)


async def _escalate_cancel(adapter, handle, *, grace_s: float, kill_s: float, now, sleep) -> str:
    """cooperative (≤grace_s) → TERM (≤kill_s total) → KILL. Returns the tier the
    runtime finally acknowledged at."""
    t0 = now()
    await _adapter_maybe(adapter, "cancel", handle)
    if await _wait_stopped(adapter, handle, deadline=t0 + grace_s, now=now, sleep=sleep):
        return "cooperative"
    await _adapter_maybe(adapter, "terminate", handle)
    if await _wait_stopped(adapter, handle, deadline=t0 + kill_s, now=now, sleep=sleep):
        return "term"
    await _adapter_maybe(adapter, "kill", handle)
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
                     now=None, sleep=asyncio.sleep) -> str:
    """Cooperatively cancel a run, escalating to TERM then KILL. Regardless of
    which tier the runtime acknowledges at, this ALWAYS revokes the run's grants,
    bumps the lease fencing epoch (fencing the orchestrator), flips the run to
    ``cancelled``, and writes a terminal ``run.failed`` with the escalation
    evidence. Returns ``"cooperative" | "term" | "kill"``. ``now`` / ``sleep`` are
    injectable so escalation windows elapse instantly in tests."""
    clock = now if now is not None else time.monotonic
    tier = await _escalate_cancel(adapter, handle, grace_s=grace_s, kill_s=kill_s,
                                  now=clock, sleep=sleep)

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
            await grants.revoke_run_grants(db, run_id)
            await _bump_epoch(db, run_id)

            run.status = "cancelled"
            run.ended_at = now
            await db.commit()
            await _emit_terminal(db, run_id, reason="kill_switch")

            stats["runs_cancelled"] += 1
            stats["grants_revoked"] += int(live_grants)

            task = await db.get(LabTask, run.task_id)
            if task is not None and task.status not in _TERMINAL_TASK_STATES:
                await lab_task_service.fail_task(db, task, reason="kill_switch")
                stats["tasks_failed"] += 1
        except Exception:  # noqa: BLE001 — isolate one bad run; the drill goes on
            logger.warning("kill_switch teardown failed for run %s", run_id, exc_info=True)
            await db.rollback()

    return stats
