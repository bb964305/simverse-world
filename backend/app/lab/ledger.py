"""Canonical event ledger + transactional outbox (PRD §Protocols, §Data and API
Evolution).

Every ``append_event`` writes, in ONE transaction, the canonical
``LabRunEvent`` (the run's append-only, gap-free log ordered by ``seq``), its
``OutboxEvent`` row (a durable, monotonic cursor for at-least-once external
delivery), and — for the handful of event types the legacy UI understands — a
``LabRunStep`` compatibility projection. Committing them together is the whole
point: a crash can never leave a canonical event without its outbox row (or a
projected step without its event), so an external consumer and the frontend can
never diverge from the ledger.

Fencing, dedup, and sequencing all fail *before* anything is written:
``expected_epoch`` routes through the Lease (a fenced writer writes nothing);
``provider_event_id`` is a dedup key (one provider event → one canonical row,
even under a concurrent insert race, via the unique-constraint fallback); and
``seq`` is unique per ``(run_id, seq)`` — a collision surfaces as
``SequenceConflict`` with the whole transaction rolled back. This module owns
event append/read/outbox/projection only; it never opens its own session and
does no WS/Redis push (``project_step`` returns the payload T7 forwards).
"""
from __future__ import annotations

from datetime import datetime, UTC

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.lab import guard, leases
from app.lab.protocol import RunEventEnvelope
from app.models.lab_event import LabRunEvent, OutboxEvent
from app.models.lab_run import LabRun, LabRunStep


class LedgerError(Exception):
    """Base for ledger-level failures."""


class SequenceConflict(LedgerError):
    """A ``(run_id, seq)`` collision — the seq was already taken. The whole
    append transaction is rolled back; nothing half-written remains."""


# Canonical event type → (legacy step phase). Only these project to a
# ``LabRunStep`` for the pre-protocol "live stream" UI; every other type is
# ledger-only (returns None from ``project_step``).
_STEP_PHASE_BY_TYPE: dict[str, str] = {
    "tool.started": "tool_call",
    "tool.completed": "observation",
    "plan.updated": "think",
    "run.started": "message",
    "run.completed": "message",
    "run.failed": "message",
}


async def next_seq(db, run_id: str) -> int:
    """The next free sequence number for ``run_id`` (MAX(seq)+1, from 1)."""
    top = (
        await db.execute(select(func.max(LabRunEvent.seq)).where(LabRunEvent.run_id == run_id))
    ).scalar_one_or_none()
    return (top or 0) + 1


def project_step(envelope: RunEventEnvelope) -> dict | None:
    """Compatibility projection of an envelope to a legacy step dict, or None if
    the event type does not map to a UI step. Summary is re-redacted defensively."""
    phase = _STEP_PHASE_BY_TYPE.get(envelope.type)
    if phase is None:
        return None
    payload = envelope.payload or {}
    tool = payload.get("tool") or payload.get("tool_name")
    summary = guard.redact_text(str(payload.get("summary", ""))) or ""
    return {"phase": phase, "tool": tool, "summary": summary}


async def append_step_projection(db, *, run, envelope: RunEventEnvelope) -> LabRunStep | None:
    """Add (not commit) a ``LabRunStep`` for a projecting event, seq = the run's
    MAX(step.seq)+1. Returns None when there is no run row or the type does not
    project. The caller's transaction (``append_event``) carries the commit."""
    if run is None:
        return None
    projected = project_step(envelope)
    if projected is None:
        return None
    top = (
        await db.execute(select(func.max(LabRunStep.seq)).where(LabRunStep.run_id == run.id))
    ).scalar_one_or_none()
    step = LabRunStep(
        run_id=run.id,
        seq=(top or 0) + 1,
        phase=projected["phase"],
        tool=projected["tool"],
        summary=projected["summary"],
    )
    db.add(step)
    return step


async def append_event(
    db,
    *,
    envelope: RunEventEnvelope,
    provider_event_id: str | None = None,
    expected_epoch: int | None = None,
    outbox_topic: str | None = None,
    commit: bool = True,
) -> LabRunEvent | None:
    """Append one canonical event + its outbox row (+ optional step projection)
    atomically. Returns the event, or None if it was deduped as a replay of an
    already-recorded ``provider_event_id``.

    Raises ``leases.StaleEpoch`` (fenced writer — nothing written) or
    ``SequenceConflict`` (seq collision — transaction rolled back).

    Reading attributes off the returned ORM object after commit is only safe
    because the session's ``sessionmaker`` uses ``expire_on_commit=False`` (true
    for both the app engine and the test factory). A caller wiring append_event
    to a default sessionmaker must re-read the row, as commit would expire it and
    attribute access would trigger a sync lazy-load.
    """
    run_id = envelope.run_id

    # 1. Fencing: a stale-epoch writer must write nothing. Check before any I/O.
    if expected_epoch is not None:
        if envelope.fencing_epoch != expected_epoch:
            raise leases.StaleEpoch(
                f"envelope epoch {envelope.fencing_epoch} != expected {expected_epoch}"
            )
        await leases.assert_epoch(db, run_id=run_id, epoch=expected_epoch)

    # 2. Idempotent dedup: one provider event → one canonical row. Read-first
    #    (fast path); the unique-constraint fallback below closes the race.
    if provider_event_id is not None:
        existing = (
            await db.execute(
                select(LabRunEvent.event_id).where(
                    LabRunEvent.run_id == run_id,
                    LabRunEvent.provider_event_id == provider_event_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return None

    # 3. The envelope carries its own seq — the caller sources it from next_seq
    #    (RunEventEnvelope.seq is Field(ge=1), so append_event never auto-assigns;
    #    the unique (run_id, seq) constraint is the gap/duplicate backstop). All
    #    reads happen before any write is queued so the step projection's MAX
    #    query cannot autoflush a half-built event.
    seq = envelope.seq
    run = await db.get(LabRun, run_id)
    payload = guard.redact_payload(envelope.payload)

    # 4. Queue the canonical event + outbox row + optional projection, then
    #    commit them as one transaction. The unique (run_id, seq) and
    #    (run_id, provider_event_id) constraints are the backstops.
    await append_step_projection(db, run=run, envelope=envelope)

    event = LabRunEvent(
        event_id=envelope.event_id,
        tenant_id=envelope.tenant_id,
        run_id=run_id,
        task_id=envelope.task_id,
        seq=seq,
        type=envelope.type,
        actor=envelope.actor,
        action_id=envelope.action_id,
        parent_id=envelope.parent_id,
        provider_event_id=provider_event_id,
        fencing_epoch=envelope.fencing_epoch,
        policy_version=envelope.policy_version,
        trace_id=envelope.trace_id,
        payload_json=payload,
        occurred_at=envelope.occurred_at,
    )
    db.add(event)

    outbox_payload = envelope.model_dump(mode="json")
    outbox_payload["payload"] = payload  # publish the redacted copy, never raw
    outbox = OutboxEvent(
        event_id=envelope.event_id,
        tenant_id=envelope.tenant_id,
        run_id=run_id,
        topic=outbox_topic or envelope.type,
        payload_json=outbox_payload,
    )
    db.add(outbox)

    try:
        if commit:
            await db.commit()
        else:
            await db.flush()
    except IntegrityError:
        await db.rollback()
        # A concurrent insert of the same provider event won the race → treat as
        # a dedup (one canonical row survives). Otherwise it is a seq collision.
        if provider_event_id is not None:
            dup = (
                await db.execute(
                    select(LabRunEvent.event_id).where(
                        LabRunEvent.run_id == run_id,
                        LabRunEvent.provider_event_id == provider_event_id,
                    )
                )
            ).scalar_one_or_none()
            if dup is not None:
                return None
        raise SequenceConflict(f"seq {seq} already exists for run {run_id}")
    return event


async def read_events(db, *, run_id: str, after_seq: int = 0, limit: int = 500) -> list[LabRunEvent]:
    """Canonical events for a run with ``seq > after_seq``, ordered by seq."""
    rows = (
        await db.execute(
            select(LabRunEvent)
            .where(LabRunEvent.run_id == run_id, LabRunEvent.seq > after_seq)
            .order_by(LabRunEvent.seq)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def read_outbox(db, *, after_id: int = 0, limit: int = 500) -> list[OutboxEvent]:
    """Outbox rows with ``id > after_id`` in id order — the durable, monotonic
    publisher cursor. Unpublished rows (``published_at`` NULL) are replayable."""
    rows = (
        await db.execute(
            select(OutboxEvent)
            .where(OutboxEvent.id > after_id)
            .order_by(OutboxEvent.id)
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


async def mark_published(db, *, outbox_ids: list[int], now: datetime | None = None) -> None:
    """Stamp ``published_at`` on the given outbox rows. Idempotent: rows already
    published keep their original timestamp (first-write-wins)."""
    if not outbox_ids:
        return
    now = now if now is not None else datetime.now(UTC)
    await db.execute(
        update(OutboxEvent)
        .where(OutboxEvent.id.in_(outbox_ids), OutboxEvent.published_at.is_(None))
        .values(published_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
