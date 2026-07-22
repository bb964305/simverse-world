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
  CONTIGUOUS committed ``provider_event_id`` in the ledger). A later committed
  cursor can never move the restart watermark past an earlier gap; exact
  resends at or below the recovered watermark are absorbed by ledger dedup.
  Every other field resets fresh. ``record_checkpoint`` /
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
from app.lab import grants, guard, leases, ledger, protocol
from app.lab.protocol import HandshakeManifest, RunEventEnvelope
from app.models.coin_hold import CoinHold
from app.models.lab_budget import LabRunBudget
from app.models.lab_event import LabRunEvent
from app.models.lab_grant import LabCapabilityGrant
from app.models.lab_lease import LabRunLease
from app.models.lab_run import LabRun
from app.models.lab_runtime import (
    LabRuntimeIntent,
    LabRuntimeResult,
    LabRuntimeSession,
    LabRuntimeTurn,
)
from app.models.lab_task import LabTask
from app.models.lab_terminalization import LabTerminalizationCommand
from app.services import lab_task_service

logger = logging.getLogger(__name__)

_ACTIVE_RUN_STATES = ("queued", "running", "needs_approval")
_TERMINAL_TASK_STATES = ("completed", "cancelled", "failed", "expired")
_ACTIVE_RUNTIME_TURN_STATES = (
    "ready",
    "intent_pending",
    "result_recorded",
    "runtime_acked",
)
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


class RuntimeProtocolConflict(SupervisionError):
    """A protocol-v2 event, ACK, or receipt changed its durable binding."""


@dataclass(frozen=True)
class RuntimeEventCommit:
    """Durable result of one RuntimeEvent ingest transaction."""

    cursor: int
    committed_through: int
    duplicate: bool
    event_id: str
    turn_row_id: str | None = None
    intent_row_id: str | None = None
    model_tokens_charged: int = 0
    budget_exhausted_dimension: str | None = None


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
    """Recover the highest contiguous committed provider cursor for ``run_id``.

    The crashed session's in-memory ACK is gone, so restart may safely ACK
    durable rows again, but it must never jump a missing cursor. Rows
    with a NULL ``provider_event_id`` (non-provider-sourced events, e.g. a
    supervisor-authored ``run.failed``) carry no cursor and are excluded."""
    cursors = (
        await db.execute(
            select(LabRunEvent.provider_event_id).where(
                LabRunEvent.run_id == run_id,
                LabRunEvent.provider_event_id.isnot(None),
            )
        )
    ).scalars().all()
    committed: set[int] = set()
    for value in cursors:
        try:
            cursor = int(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeProtocolConflict(
                "provider ledger contains a malformed cursor"
            ) from exc
        if cursor <= 0:
            raise RuntimeProtocolConflict(
                "provider ledger contains a non-positive cursor"
            )
        committed.add(cursor)
    contiguous = 0
    while contiguous + 1 in committed:
        contiguous += 1
    return contiguous


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


# ── protocol-v2 durable event / ACK / result supervision ─────────────

def _database_clock(db):
    if db.get_bind().dialect.name == "postgresql":
        return func.clock_timestamp()
    return func.current_timestamp()


async def _lock_v2_authority(
    db,
    *,
    run_id: str,
    session_id: str,
    epoch: int,
    owner_id: str,
    provider_binding: bool = True,
) -> LabRuntimeSession:
    """Lock the live lease and its exact protocol-v2 session binding."""
    if not isinstance(owner_id, str) or not owner_id:
        raise RuntimeProtocolConflict("runtime supervision owner is required")
    candidates = (
        await db.execute(
            select(LabRuntimeSession).where(
                LabRuntimeSession.run_id == run_id,
                (
                    (LabRuntimeSession.id == session_id)
                    | (LabRuntimeSession.provider_session_id == session_id)
                ),
            )
        )
    ).scalars().all()
    if len(candidates) != 1:
        raise RuntimeProtocolConflict("runtime session binding is ambiguous or missing")
    candidate = candidates[0]
    authority_epoch = candidate.authority_epoch
    lease = await db.scalar(
        select(LabRunLease)
        .where(
            LabRunLease.run_id == run_id,
            LabRunLease.owner_id == owner_id,
            LabRunLease.fencing_epoch == authority_epoch,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if lease is None:
        raise RuntimeProtocolConflict("runtime supervision lease binding mismatch")
    live = await db.scalar(
        select(LabRunLease.run_id).where(
            LabRunLease.run_id == run_id,
            LabRunLease.owner_id == owner_id,
            LabRunLease.fencing_epoch == authority_epoch,
            LabRunLease.expires_at > _database_clock(db),
        )
    )
    if live is None:
        raise RuntimeProtocolConflict("runtime supervision lease is expired")

    session = (
        await db.execute(
        select(LabRuntimeSession)
        .where(
            LabRuntimeSession.id == candidate.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (
        session is None
        or session.run_id != run_id
        or session.fencing_epoch != epoch
        or session.authority_epoch != authority_epoch
        or session.protocol_version != protocol.PROTOCOL_V2
        or (
            provider_binding
            and session.provider_session_id != session_id
        )
        or (
            not provider_binding
            and session.id != session_id
        )
    ):
        raise RuntimeProtocolConflict("runtime session binding mismatch")
    await _assert_v2_authority_live(
        db,
        session=session,
        owner_id=owner_id,
    )
    return session


async def _assert_v2_authority_live(
    db, *, session: LabRuntimeSession, owner_id: str
) -> None:
    live = await db.scalar(
        select(LabRunLease.run_id).where(
            LabRunLease.run_id == session.run_id,
            LabRunLease.owner_id == owner_id,
            LabRunLease.fencing_epoch == session.authority_epoch,
            LabRunLease.expires_at > _database_clock(db),
        )
    )
    if live is None:
        raise RuntimeProtocolConflict(
            "runtime supervision lease expired while waiting for state lock"
        )


async def lock_runtime_authority(
    db,
    *,
    run_id: str,
    session_id: str,
    epoch: int,
    owner_id: str,
    provider_binding: bool = True,
) -> LabRuntimeSession:
    """Shared Broker/orchestrator authority lock for protocol-v2 writes."""
    return await _lock_v2_authority(
        db,
        run_id=run_id,
        session_id=session_id,
        epoch=epoch,
        owner_id=owner_id,
        provider_binding=provider_binding,
    )


async def assert_runtime_authority_live(
    db, *, session: LabRuntimeSession, owner_id: str
) -> None:
    await _assert_v2_authority_live(db, session=session, owner_id=owner_id)


def _runtime_event_digest(event: protocol.RuntimeEvent) -> str:
    return protocol.content_digest(event.model_dump(mode="json"))


def _runtime_event_ledger_type(event_kind: str) -> str:
    return {
        "tool_intent": "tool.requested",
        "tool_result": "tool.completed",
        "observation": "tool.completed",
        "cancelled": "run.failed",
        "failed": "run.failed",
    }.get(event_kind, "plan.updated")


def _runtime_event_ledger_payload(
    event: protocol.RuntimeEvent,
    *,
    event_digest: str,
    event_bytes: int,
    model_tokens_charged: int,
    budget_exhausted_dimension: str | None,
) -> dict:
    payload = dict(event.payload or {})
    summary = payload.get("summary")
    canonical = {
        "runtime_event_digest": event_digest,
        "runtime_event_bytes": event_bytes,
        # Avoid secret-like key names here: the ledger's recursive redactor
        # intentionally masks any key containing "token".
        "runtime_model_usage_charged": model_tokens_charged,
        "runtime_budget_exhausted_dimension": budget_exhausted_dimension,
        "runtime_event_id": event.event_id,
        "runtime_session_id": event.session_id,
        "provider_cursor": event.cursor,
        "event_kind": event.event_kind,
        "turn_id": event.turn_id,
        "intent_id": event.intent_id,
        "outcome": event.outcome,
        "payload": payload,
    }
    if summary is not None:
        canonical["summary"] = summary
    if event.event_kind == "tool_intent":
        canonical.update(
            tool=event.tool_name,
            tool_name=event.tool_name,
            tool_args_digest=event.tool_args_digest,
        )
    return canonical


async def _debit_runtime_model_tokens(
    db, *, event: protocol.RuntimeEvent
) -> tuple[int, str | None]:
    """Debit one canonical think event in the same transaction as its ledger row."""
    if event.event_kind != "think":
        return 0, None
    model_tokens = (event.payload or {}).get("model_tokens", 0)
    if type(model_tokens) is not int or model_tokens < 0:
        raise RuntimeProtocolConflict(
            "Runtime think event model_tokens must be a non-negative integer"
        )
    if model_tokens == 0:
        return 0, None
    budget = await db.scalar(
        select(LabRunBudget)
        .where(LabRunBudget.run_id == event.run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if budget is None:
        return 0, None
    if budget.exhausted_dimension is not None:
        return 0, budget.exhausted_dimension
    projected = (
        budget.used_model_tokens
        + budget.reserved_model_tokens
        + model_tokens
    )
    if budget.limit_model_tokens and projected > budget.limit_model_tokens:
        budget.exhausted_dimension = "model_tokens"
        return 0, "model_tokens"
    budget.used_model_tokens += model_tokens
    return model_tokens, None


async def _unacked_window(
    db, *, run_id: str, acked_through: int
) -> tuple[int, int]:
    rows = (
        await db.execute(
            select(LabRunEvent.provider_event_id, LabRunEvent.payload_json).where(
                LabRunEvent.run_id == run_id,
                LabRunEvent.provider_event_id.isnot(None),
            )
        )
    ).all()
    event_sizes = []
    for raw_cursor, payload in rows:
        try:
            cursor = int(raw_cursor)
        except (TypeError, ValueError) as exc:
            raise RuntimeProtocolConflict(
                "protocol-v2 run contains a malformed provider cursor"
            ) from exc
        if cursor > acked_through:
            event_bytes = (payload or {}).get("runtime_event_bytes")
            if (
                type(event_bytes) is not int
                or event_bytes <= 0
                or event_bytes > protocol.MAX_EVENT_BYTES
            ):
                raise RuntimeProtocolConflict(
                    "protocol-v2 ledger is missing an exact event byte count"
                )
            event_sizes.append(event_bytes)
    return len(event_sizes), sum(event_sizes)


async def _highest_contiguous_runtime_cursor(
    db, *, run_id: str, after: int
) -> int:
    values = (
        await db.execute(
            select(LabRunEvent.provider_event_id).where(
                LabRunEvent.run_id == run_id,
                LabRunEvent.provider_event_id.isnot(None),
            )
        )
    ).scalars().all()
    cursors: set[int] = set()
    for value in values:
        try:
            cursors.add(int(value))
        except (TypeError, ValueError) as exc:
            raise RuntimeProtocolConflict(
                "protocol-v2 run contains a malformed provider cursor"
            ) from exc
    contiguous = after
    while contiguous + 1 in cursors:
        contiguous += 1
    return contiguous


async def runtime_final_ready(
    db,
    *,
    session_id: str,
    require_real_result: bool = True,
    require_succeeded: bool = True,
) -> bool:
    """Return whether Gateway truth permits accepting Runtime final output."""
    intent_count = await db.scalar(
        select(func.count())
        .select_from(LabRuntimeIntent)
        .where(LabRuntimeIntent.session_id == session_id)
    )
    unresolved_intents = await db.scalar(
        select(func.count())
        .select_from(LabRuntimeIntent)
        .where(
            LabRuntimeIntent.session_id == session_id,
            LabRuntimeIntent.status != "runtime_acked",
        )
    )
    result_count = await db.scalar(
        select(func.count())
        .select_from(LabRuntimeResult)
        .where(LabRuntimeResult.session_id == session_id)
    )
    unacked_results = await db.scalar(
        select(func.count())
        .select_from(LabRuntimeResult)
        .where(
            LabRuntimeResult.session_id == session_id,
            LabRuntimeResult.runtime_acked_at.is_(None),
        )
    )
    if require_real_result and not result_count:
        return False
    non_success_results = 0
    if require_succeeded:
        non_success_results = await db.scalar(
            select(func.count())
            .select_from(LabRuntimeResult)
            .where(
                LabRuntimeResult.session_id == session_id,
                LabRuntimeResult.outcome != "succeeded",
            )
        )
    return (
        unresolved_intents == 0
        and unacked_results == 0
        and result_count == intent_count
        and non_success_results == 0
    )


async def runtime_read_window(
    db, *, session_id: str
) -> tuple[int, int, int]:
    """Return ``(after, remaining_events, remaining_bytes)`` for provider poll."""
    session = await db.get(LabRuntimeSession, session_id)
    if session is None:
        raise RuntimeProtocolConflict("runtime session does not exist")
    unacked_events, unacked_bytes = await _unacked_window(
        db,
        run_id=session.run_id,
        acked_through=session.provider_cursor_acked,
    )
    remaining_events = protocol.MAX_UNACKED_EVENTS - unacked_events
    remaining_bytes = protocol.MAX_UNACKED_BYTES - unacked_bytes
    if remaining_events <= 0 or remaining_bytes <= 0:
        raise Backpressure(unacked_events, unacked_bytes)
    return session.provider_cursor_acked, remaining_events, remaining_bytes


async def validate_runtime_provider(provider):
    """Require the complete P3 provider proof and concrete result-loop hooks."""
    from pydantic import ValidationError

    handshake = getattr(provider, "supervision_handshake", None)
    if not callable(handshake):
        raise HandshakeRejected("Runtime provider has no supervision handshake")
    try:
        proof = await handshake()
        proof = protocol.RuntimeV2SupervisionHandshake.model_validate(
            proof, strict=True
        )
    except (ValidationError, ValueError, TypeError) as exc:
        raise HandshakeRejected("Runtime provider supervision proof is invalid") from exc
    required_hooks = (
        "create_session",
        "reattach_session",
        "submit_goal_v2",
        "read_runtime_events",
        "ack_runtime_events",
        "send_runtime_result",
        "collect_artifacts_v2",
    )
    missing = [name for name in required_hooks if not callable(getattr(provider, name, None))]
    if missing:
        raise HandshakeRejected(
            "Runtime provider is missing result-loop hooks: " + ", ".join(missing)
        )
    return proof


async def _commit_runtime_event(
    db,
    *,
    event: protocol.RuntimeEvent,
    owner_id: str,
) -> RuntimeEventCommit:
    """Commit one RuntimeEvent and all canonical state before transport ACK.

    The session row serializes cursor advancement. Replayed cursors must carry
    the exact same protocol digest; gaps may be stored, but the committed
    watermark advances only when the gap closes.
    """
    if not isinstance(event, protocol.RuntimeEvent):
        event = protocol.RuntimeEvent.model_validate(event, strict=True)
    session = await _lock_v2_authority(
        db,
        run_id=event.run_id,
        session_id=event.session_id,
        epoch=event.epoch,
        owner_id=owner_id,
    )
    digest = _runtime_event_digest(event)
    existing = await db.scalar(
        select(LabRunEvent).where(
            LabRunEvent.run_id == event.run_id,
            LabRunEvent.provider_event_id == str(event.cursor),
        )
    )
    if existing is not None:
        if (existing.payload_json or {}).get("runtime_event_digest") != digest:
            raise RuntimeProtocolConflict(
                "provider cursor replay changed its RuntimeEvent binding"
            )
        turn = None
        intent = None
        if event.turn_id:
            turn = await db.scalar(
                select(LabRuntimeTurn).where(
                    LabRuntimeTurn.session_id == session.id,
                    LabRuntimeTurn.turn_id == event.turn_id,
                )
            )
        if event.intent_id:
            intent = await db.scalar(
                select(LabRuntimeIntent).where(
                    LabRuntimeIntent.session_id == session.id,
                    LabRuntimeIntent.intent_id == event.intent_id,
                )
            )
        await _assert_v2_authority_live(db, session=session, owner_id=owner_id)
        await db.commit()
        return RuntimeEventCommit(
            cursor=event.cursor,
            committed_through=session.provider_cursor_committed,
            duplicate=True,
            event_id=existing.event_id,
            turn_row_id=turn.id if turn else None,
            intent_row_id=intent.id if intent else None,
            budget_exhausted_dimension=(existing.payload_json or {}).get(
                "runtime_budget_exhausted_dimension"
            ),
        )
    if event.cursor <= session.provider_cursor_committed:
        raise RuntimeProtocolConflict("provider cursor regressed without an exact replay")
    if session.status != "ready":
        raise RuntimeProtocolConflict(
            f"runtime session cannot ingest from state {session.status}"
        )

    blocking_intent_cursor = await db.scalar(
        select(LabRuntimeIntent.provider_cursor).where(
            LabRuntimeIntent.session_id == session.id,
            LabRuntimeIntent.status.in_(("pending", "result_recorded")),
        )
    )
    if (
        blocking_intent_cursor is not None
        and event.cursor > blocking_intent_cursor
    ):
        raise RuntimeProtocolConflict(
            "runtime advanced while a Gateway result was not acknowledged"
        )

    unacked_events, unacked_bytes = await _unacked_window(
        db,
        run_id=event.run_id,
        acked_through=session.provider_cursor_acked,
    )
    incoming_size = len(
        protocol.canonical_json(event.model_dump(mode="json")).encode("utf-8")
    )
    if incoming_size > protocol.MAX_EVENT_BYTES:
        raise RuntimeProtocolConflict("Runtime event exceeds the single-event byte cap")
    if (
        unacked_events >= protocol.MAX_UNACKED_EVENTS
        or unacked_bytes + incoming_size > protocol.MAX_UNACKED_BYTES
    ):
        raise Backpressure(unacked_events, unacked_bytes)
    closes_next_gap = event.cursor == session.provider_cursor_committed + 1
    if not closes_next_gap and (
        unacked_events + 1 >= protocol.MAX_UNACKED_EVENTS
        or (
            unacked_bytes
            + incoming_size
            + protocol.MAX_EVENT_BYTES
            > protocol.MAX_UNACKED_BYTES
        )
    ):
        # Preserve one event slot and one maximum-sized event for the missing
        # contiguous cursor. Otherwise out-of-order highs can fill the durable
        # window and make the gap impossible to ingest or ACK.
        raise Backpressure(unacked_events, unacked_bytes)

    model_tokens_charged, budget_exhausted_dimension = (
        await _debit_runtime_model_tokens(db, event=event)
    )

    turn = None
    intent = None
    if event.turn_id is not None:
        turn = await db.scalar(
            select(LabRuntimeTurn).where(
                LabRuntimeTurn.session_id == session.id,
                LabRuntimeTurn.turn_id == event.turn_id,
            )
        )
        if turn is None:
            if event.event_kind == "final":
                raise RuntimeProtocolConflict(
                    "runtime final must bind the active turn"
                )
            active_turn = await db.scalar(
                select(LabRuntimeTurn.id).where(
                    LabRuntimeTurn.session_id == session.id,
                    LabRuntimeTurn.status.in_(_ACTIVE_RUNTIME_TURN_STATES),
                )
            )
            if active_turn is not None:
                raise RuntimeProtocolConflict(
                    "runtime cannot create a new turn while another is active"
                )
            sequence = (
                await db.scalar(
                    select(func.count())
                    .select_from(LabRuntimeTurn)
                    .where(LabRuntimeTurn.session_id == session.id)
                )
                or 0
            ) + 1
            turn = LabRuntimeTurn(
                session_id=session.id,
                turn_id=event.turn_id,
                sequence=sequence,
                status="ready",
                provider_cursor=event.cursor,
            )
            db.add(turn)
            await db.flush()
        elif (
            event.event_kind
            in {"think", "tool_intent", "tool_result", "observation", "final"}
            and turn.status not in _ACTIVE_RUNTIME_TURN_STATES
        ):
            raise RuntimeProtocolConflict(
                "runtime event cannot advance a terminal turn"
            )

    if event.event_kind == "tool_intent":
        assert turn is not None and event.intent_id is not None
        existing_intent = await db.scalar(
            select(LabRuntimeIntent).where(
                LabRuntimeIntent.session_id == session.id,
                LabRuntimeIntent.intent_id == event.intent_id,
            )
        )
        if existing_intent is not None:
            raise RuntimeProtocolConflict(
                "runtime intent was rebound to a different provider cursor"
            )
        pending = await db.scalar(
            select(LabRuntimeIntent.id).where(
                LabRuntimeIntent.session_id == session.id,
                LabRuntimeIntent.status.in_(("pending", "result_recorded")),
            )
        )
        if pending is not None or turn.status != "ready":
            raise RuntimeProtocolConflict("runtime emitted a second pending intent")
        intent = LabRuntimeIntent(
            session_id=session.id,
            runtime_turn_id=turn.id,
            intent_id=event.intent_id,
            action_id=None,
            tool_name=event.tool_name,
            args_digest=event.tool_args_digest,
            args_redacted_json=guard.redact_payload(event.tool_args),
            status="pending",
            provider_cursor=event.cursor,
            fencing_epoch=event.epoch,
        )
        db.add(intent)
        turn.status = "intent_pending"
    elif event.event_kind in {"tool_result", "observation"}:
        assert event.intent_id is not None and turn is not None
        intent = await db.scalar(
            select(LabRuntimeIntent).where(
                LabRuntimeIntent.session_id == session.id,
                LabRuntimeIntent.runtime_turn_id == turn.id,
                LabRuntimeIntent.intent_id == event.intent_id,
            )
        )
        if intent is None or intent.status != "runtime_acked":
            raise RuntimeProtocolConflict(
                "runtime result event has no runtime-acked Gateway command"
            )
        result = await db.scalar(
            select(LabRuntimeResult).where(
                LabRuntimeResult.runtime_intent_id == intent.id,
                LabRuntimeResult.runtime_acked_at.isnot(None),
            )
        )
        if (
            result is None
            or result.runtime_turn_id != turn.id
            or result.intent_id != event.intent_id
            or result.fencing_epoch != event.epoch
            or result.payload_json != event.payload
            or result.result_digest != protocol.content_digest(event.payload)
            or (
                event.event_kind == "tool_result"
                and result.outcome != event.outcome
            )
        ):
            raise RuntimeProtocolConflict(
                "runtime result event changed the persisted Broker outcome"
            )
        turn.status = "completed"
        turn.completed_at = datetime.now(UTC)
    elif event.event_kind == "final":
        if turn is None:
            raise RuntimeProtocolConflict("runtime final event requires a turn")
        if not await runtime_final_ready(
            db,
            session_id=session.id,
            require_real_result=True,
            require_succeeded=False,
        ):
            raise RuntimeProtocolConflict(
                "runtime final is blocked by pending or unacked results"
            )
        turn.status = "final"
        turn.final_digest = protocol.content_digest(event.payload)
        turn.completed_at = datetime.now(UTC)
        success_ready = await runtime_final_ready(
            db,
            session_id=session.id,
            require_real_result=True,
            require_succeeded=True,
        )
        session.status = "completed" if success_ready else "failed"
        session.ended_at = datetime.now(UTC)
        if not success_ready:
            session.last_error = "runtime final followed a denied or failed Broker result"
    elif event.event_kind in {"cancelled", "failed"}:
        session.status = "cancelled" if event.event_kind == "cancelled" else "failed"
        session.ended_at = datetime.now(UTC)
        session.last_error = str((event.payload or {}).get("reason") or event.event_kind)[:500]

    action_id = intent.action_id if intent is not None else None
    run = await db.get(LabRun, event.run_id)
    if run is None:
        raise RuntimeProtocolConflict("runtime event run does not exist")
    task = await db.get(LabTask, run.task_id)
    if (
        task is None
        or not isinstance(task.issuer_user_id, str)
        or not task.issuer_user_id
    ):
        raise RuntimeProtocolConflict(
            "runtime event has no authoritative task tenant binding"
        )
    seq = await ledger.next_seq(db, event.run_id)
    envelope = RunEventEnvelope(
        event_id=str(uuid.uuid4()),
        tenant_id=task.issuer_user_id,
        run_id=event.run_id,
        task_id=run.task_id,
        seq=seq,
        type=_runtime_event_ledger_type(event.event_kind),
        actor="runtime",
        action_id=action_id,
        fencing_epoch=session.authority_epoch,
        policy_version=settings.lab_policy_version,
        occurred_at=event.occurred_at,
        payload=_runtime_event_ledger_payload(
            event,
            event_digest=digest,
            event_bytes=incoming_size,
            model_tokens_charged=model_tokens_charged,
            budget_exhausted_dimension=budget_exhausted_dimension,
        ),
    )
    canonical = await ledger.append_event(
        db,
        envelope=envelope,
        provider_event_id=str(event.cursor),
        expected_epoch=session.authority_epoch,
        outbox_topic="lab_run_event",
        commit=False,
    )
    if canonical is None:
        raise RuntimeProtocolConflict("provider cursor raced with a divergent ingest")
    await db.flush()
    session.provider_cursor_committed = await _highest_contiguous_runtime_cursor(
        db,
        run_id=event.run_id,
        after=session.provider_cursor_committed,
    )
    session.updated_at = datetime.now(UTC)
    await _assert_v2_authority_live(db, session=session, owner_id=owner_id)
    await db.commit()
    return RuntimeEventCommit(
        cursor=event.cursor,
        committed_through=session.provider_cursor_committed,
        duplicate=False,
        event_id=canonical.event_id,
        turn_row_id=turn.id if turn else None,
        intent_row_id=intent.id if intent else None,
        model_tokens_charged=model_tokens_charged,
        budget_exhausted_dimension=budget_exhausted_dimension,
    )


async def commit_runtime_event(
    db,
    *,
    event: protocol.RuntimeEvent,
    owner_id: str,
) -> RuntimeEventCommit:
    try:
        return await _commit_runtime_event(db, event=event, owner_id=owner_id)
    except BaseException:
        await db.rollback()
        raise


async def _record_provider_ack(
    db,
    *,
    run_id: str,
    session_id: str,
    epoch: int,
    owner_id: str,
    acked_through: int,
) -> int:
    """Persist a provider ACK only after its authenticated transport succeeds."""
    if type(acked_through) is not int or acked_through < 0:
        raise RuntimeProtocolConflict("provider ACK cursor is invalid")
    session = await _lock_v2_authority(
        db,
        run_id=run_id,
        session_id=session_id,
        epoch=epoch,
        owner_id=owner_id,
    )
    if acked_through > session.provider_cursor_committed:
        raise RuntimeProtocolConflict("provider ACK exceeds committed cursor")
    if acked_through < session.provider_cursor_acked:
        raise RuntimeProtocolConflict("provider ACK cursor regressed")
    if acked_through > session.provider_cursor_acked:
        session.provider_cursor_acked = acked_through
        session.updated_at = datetime.now(UTC)
    await _assert_v2_authority_live(db, session=session, owner_id=owner_id)
    await db.commit()
    return session.provider_cursor_acked


async def record_provider_ack(
    db,
    *,
    run_id: str,
    session_id: str,
    epoch: int,
    owner_id: str,
    acked_through: int,
) -> int:
    try:
        return await _record_provider_ack(
            db,
            run_id=run_id,
            session_id=session_id,
            epoch=epoch,
            owner_id=owner_id,
            acked_through=acked_through,
        )
    except BaseException:
        await db.rollback()
        raise


async def record_completed_provider_ack_recovery(
    db,
    *,
    run_id: str,
    session_id: str,
    runtime_epoch: int,
    authority_epoch: int,
    owner_id: str,
    acked_through: int,
) -> int:
    """Persist ACK catch-up for an older, already-final Runtime session."""
    try:
        if (
            type(acked_through) is not int
            or acked_through < 0
            or type(runtime_epoch) is not int
            or type(authority_epoch) is not int
            or authority_epoch <= runtime_epoch
        ):
            raise RuntimeProtocolConflict(
                "completed Runtime ACK recovery binding is invalid"
            )
        session = await db.scalar(
            select(LabRuntimeSession)
            .where(
                LabRuntimeSession.run_id == run_id,
                LabRuntimeSession.provider_session_id == session_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            session is None
            or session.status != "completed"
            or session.fencing_epoch != runtime_epoch
            or session.protocol_version != protocol.PROTOCOL_V2
        ):
            raise RuntimeProtocolConflict(
                "completed Runtime ACK recovery session mismatch"
            )
        live = await db.scalar(
            select(LabRunLease.run_id).where(
                LabRunLease.run_id == run_id,
                LabRunLease.owner_id == owner_id,
                LabRunLease.fencing_epoch == authority_epoch,
                LabRunLease.expires_at > _database_clock(db),
            )
        )
        if live is None:
            raise RuntimeProtocolConflict(
                "completed Runtime ACK recovery lease is not live"
            )
        if not await runtime_final_ready(
            db,
            session_id=session.id,
            require_real_result=True,
            require_succeeded=True,
        ):
            raise RuntimeProtocolConflict(
                "completed Runtime ACK recovery is not success-ready"
            )
        if acked_through > session.provider_cursor_committed:
            raise RuntimeProtocolConflict("provider ACK exceeds committed cursor")
        if acked_through < session.provider_cursor_acked:
            raise RuntimeProtocolConflict("provider ACK cursor regressed")
        if acked_through > session.provider_cursor_acked:
            session.provider_cursor_acked = acked_through
            session.updated_at = datetime.now(UTC)
        live = await db.scalar(
            select(LabRunLease.run_id).where(
                LabRunLease.run_id == run_id,
                LabRunLease.owner_id == owner_id,
                LabRunLease.fencing_epoch == authority_epoch,
                LabRunLease.expires_at > _database_clock(db),
            )
        )
        if live is None:
            raise RuntimeProtocolConflict(
                "completed Runtime ACK recovery lease expired before commit"
            )
        await db.commit()
        return session.provider_cursor_acked
    except BaseException:
        await db.rollback()
        raise


async def _record_runtime_result_receipt(
    db,
    *,
    command: protocol.ToolResultCommand,
    receipt: dict,
    owner_id: str,
) -> LabRuntimeResult:
    """CAS one exact Runtime receipt into Gateway result/intent/turn truth."""
    session = await _lock_v2_authority(
        db,
        run_id=command.run_id,
        session_id=command.session_id,
        epoch=command.epoch,
        owner_id=owner_id,
    )
    if session.status not in {"ready", "completed"}:
        raise RuntimeProtocolConflict(
            f"runtime result receipt is invalid in session state {session.status}"
        )
    expected_digest = protocol.content_digest(command.model_dump(mode="json"))
    receipt_id = receipt.get("receipt_id") if isinstance(receipt, dict) else None
    if not isinstance(receipt_id, str) or not receipt_id:
        raise RuntimeProtocolConflict("runtime result receipt_id is missing")
    if receipt.get("request_digest") != expected_digest:
        raise RuntimeProtocolConflict("runtime result receipt digest mismatch")
    expected_fields = {
        "session_id": command.session_id,
        "turn_id": command.turn_id,
        "intent_id": command.intent_id,
        "action_id": command.action_id,
    }
    if any(receipt.get(name) != value for name, value in expected_fields.items()):
        raise RuntimeProtocolConflict("runtime result receipt binding mismatch")
    if receipt.get("state") != "runtime_acked":
        raise RuntimeProtocolConflict("runtime did not acknowledge the result command")

    result = await db.scalar(
        select(LabRuntimeResult)
        .where(LabRuntimeResult.command_id == command.command_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if result is None:
        raise RuntimeProtocolConflict("runtime result command was not persisted")
    if (
        result.session_id != session.id
        or result.intent_id != command.intent_id
        or result.action_id != command.action_id
        or result.fencing_epoch != command.epoch
        or result.request_digest != expected_digest
        or result.result_digest != command.result_digest
        or result.outcome != command.outcome
        or result.payload_json != command.payload
    ):
        raise RuntimeProtocolConflict("runtime result row binding mismatch")
    if result.runtime_acked_at is not None:
        if result.receipt_id != receipt_id:
            raise RuntimeProtocolConflict("runtime retry returned a different receipt")
        await _assert_v2_authority_live(db, session=session, owner_id=owner_id)
        await db.commit()
        return result

    intent = await db.scalar(
        select(LabRuntimeIntent)
        .where(LabRuntimeIntent.id == result.runtime_intent_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    turn = await db.scalar(
        select(LabRuntimeTurn)
        .where(LabRuntimeTurn.id == result.runtime_turn_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        intent is None
        or turn is None
        or intent.status != "result_recorded"
        or turn.status != "result_recorded"
    ):
        raise RuntimeProtocolConflict("runtime result state transition is invalid")
    result.receipt_id = receipt_id
    result.runtime_acked_at = datetime.now(UTC)
    intent.status = "runtime_acked"
    turn.status = "runtime_acked"
    await _assert_v2_authority_live(db, session=session, owner_id=owner_id)
    await db.commit()
    return result


async def record_runtime_result_receipt(
    db,
    *,
    command: protocol.ToolResultCommand,
    receipt: dict,
    owner_id: str,
) -> LabRuntimeResult:
    try:
        return await _record_runtime_result_receipt(
            db,
            command=command,
            receipt=receipt,
            owner_id=owner_id,
        )
    except BaseException:
        await db.rollback()
        raise


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


async def _bump_epoch(db, run_id: str, *, commit: bool = True) -> int:
    """Structurally fence the current owner: a direct ``fencing_epoch += 1`` on
    the lease (takeover semantics). The old owner's next heartbeat/emit carries
    the stale epoch → rejected. No lease row (never started) → epoch stays 0."""
    await db.execute(
        update(LabRunLease)
        .where(LabRunLease.run_id == run_id)
        .values(fencing_epoch=LabRunLease.fencing_epoch + 1)
        .execution_options(synchronize_session=False)
    )
    if commit:
        await db.commit()
    else:
        await db.flush()
    return await leases.current_epoch(db, run_id)


async def _emit_terminal(
    db,
    run_id: str,
    *,
    reason: str,
    extra: dict | None = None,
    event_id: str | None = None,
    commit: bool = True,
) -> None:
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
        event_id=event_id or str(uuid.uuid4()), tenant_id=tenant_id, run_id=run_id, task_id=run.task_id,
        seq=seq, type="run.failed", actor="supervisor", fencing_epoch=epoch,
        policy_version=settings.lab_policy_version, occurred_at=datetime.now(UTC),
        payload=payload,
    )
    await ledger.append_event(
        db,
        envelope=envelope,
        outbox_topic="lab_run_event",
        commit=commit,
    )


def _supervisor_terminal_event_id(run_id: str, reason: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"simverse:lab:supervisor-terminal:{run_id}:{reason}",
        )
    )


async def _ensure_supervisor_terminal_event(
    db,
    *,
    run_id: str,
    reason: str,
    extra: dict | None = None,
) -> bool:
    """Converge the audit event after the safety fence is already durable."""
    event_id = _supervisor_terminal_event_id(run_id, reason)
    for attempt in range(3):
        if await db.get(LabRunEvent, event_id) is not None:
            return True
        try:
            await _emit_terminal(
                db,
                run_id,
                reason=reason,
                extra=extra,
                event_id=event_id,
            )
            return True
        except Exception:  # noqa: BLE001 - the durable fence must not roll back
            await db.rollback()
            if await db.get(LabRunEvent, event_id) is not None:
                return True
            if attempt == 2:
                logger.error(
                    "supervisor terminal event did not converge for run %s",
                    run_id,
                    exc_info=True,
                )
    return False


async def _fence_run_once(
    db,
    *,
    run_id: str,
    ended_at: datetime,
    reason: str,
    extra: dict | None = None,
) -> tuple[bool, int]:
    """Atomically fence one active run; converge its audit event afterwards."""
    run = (
        await db.execute(
            select(LabRun)
            .where(LabRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None or run.status not in _ACTIVE_RUN_STATES:
        should_recover_event = (
            run is not None and run.status == "cancelled" and run.error == reason
        )
        await db.commit()
        if should_recover_event:
            await _ensure_supervisor_terminal_event(
                db,
                run_id=run_id,
                reason=reason,
                extra=extra,
            )
        return False, 0

    live_grants = (
        await db.execute(
            select(func.count()).select_from(LabCapabilityGrant).where(
                LabCapabilityGrant.run_id == run_id,
                LabCapabilityGrant.revoked_at.is_(None),
            )
        )
    ).scalar_one()
    await grants.revoke_run_grants(db, run_id, commit=False)
    await _bump_epoch(db, run_id, commit=False)
    run.status = "cancelled"
    run.ended_at = ended_at
    run.error = reason
    await db.commit()
    await _ensure_supervisor_terminal_event(
        db,
        run_id=run_id,
        reason=reason,
        extra=extra,
    )
    return True, int(live_grants)


async def reconcile_cancelled_run_event(db, *, run_id: str) -> bool:
    """Recover an audit event after a crash between fence commit and append."""
    run = await db.get(LabRun, run_id)
    if (
        run is None
        or run.status != "cancelled"
        or not run.error
        or not (run.error == "kill_switch" or run.error.startswith("cancelled:"))
    ):
        return False
    extra = None
    if run.error.startswith("cancelled:"):
        extra = {"escalation": "unknown_after_recovery"}
    return await _ensure_supervisor_terminal_event(
        db,
        run_id=run_id,
        reason=run.error,
        extra=extra,
    )


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
        # Fencing lands under any adapter outcome, and only one concurrent
        # control request may advance the epoch or emit the terminal event.
        await _fence_run_once(
            db,
            run_id=run_id,
            ended_at=datetime.now(UTC),
            reason=f"cancelled:{reason}",
            extra={"escalation": tier},
        )
    return tier


# ── kill-switch drill ─────────────────────────────────────────────────

async def kill_switch_all(db, *, now: datetime | None = None) -> dict:
    """Terminate every active run (``queued`` / ``running`` / ``needs_approval``):
    flip it to ``cancelled``, revoke its grants, fence its lease, refund the task,
    and write a ``run.failed(reason="kill_switch")``. A cancelled run whose
    escrow command was never persisted is retried without repeating the fence;
    once a command exists, later calls are strict no-ops. Per-run failures are
    isolated so one bad run cannot abort the drill.

    Follows ``expire_lab_tasks``'s single-session, commit-per-item idiom (the
    brief's ``db_factory`` sketch is superseded: a passed session is testable both
    directly and from the admin route's request scope, and keeps this module's
    "never opens its own session" contract)."""
    now = now if now is not None else datetime.now(UTC)
    failed_terminalizations = (
        await db.execute(
            select(func.count())
            .select_from(LabTerminalizationCommand)
            .join(LabTask, LabTask.id == LabTerminalizationCommand.task_id)
            .join(CoinHold, CoinHold.id == LabTerminalizationCommand.hold_id)
            .where(
                LabTerminalizationCommand.operation == "fail",
                LabTerminalizationCommand.status == "failed",
                LabTask.status.not_in(_TERMINAL_TASK_STATES),
                CoinHold.status == "held",
            )
        )
    ).scalar_one()
    stats = {
        "runs_cancelled": 0,
        "grants_revoked": 0,
        "tasks_failed": 0,
        "terminalization_failed": int(failed_terminalizations),
    }

    active = (await db.execute(
        select(LabRun).where(LabRun.status.in_(_ACTIVE_RUN_STATES))
    )).scalars().all()
    retryable = (
        await db.execute(
            select(LabRun)
            .join(LabTask, LabTask.id == LabRun.task_id)
            .join(CoinHold, CoinHold.id == LabTask.hold_id)
            .where(
                LabRun.status == "cancelled",
                LabTask.status.not_in(_TERMINAL_TASK_STATES),
                CoinHold.status == "held",
                ~select(LabTerminalizationCommand.command_id)
                .where(
                    LabTerminalizationCommand.task_id == LabTask.id,
                    LabTerminalizationCommand.hold_id == CoinHold.id,
                    LabTerminalizationCommand.operation == "fail",
                )
                .exists(),
            )
        )
    ).scalars().all()
    event_recovery: list[LabRun] = []
    kill_marked = (
        await db.execute(
            select(LabRun).where(
                LabRun.status == "cancelled",
                LabRun.error == "kill_switch",
            )
        )
    ).scalars().all()
    for run in kill_marked:
        event_id = _supervisor_terminal_event_id(run.id, "kill_switch")
        if await db.get(LabRunEvent, event_id) is None:
            event_recovery.append(run)

    candidates: list[tuple[LabRun, bool]] = [(run, True) for run in active]
    seen_run_ids = {run.id for run in active}
    for run in retryable:
        if run.id not in seen_run_ids:
            candidates.append((run, False))
            seen_run_ids.add(run.id)
    for run in event_recovery:
        if run.id not in seen_run_ids:
            candidates.append((run, False))
            seen_run_ids.add(run.id)

    for run, needs_fence in candidates:
        run_id = run.id
        task_id = run.task_id
        try:
            terminalization_requested = False
            if needs_fence:
                fenced, live_grants = await _fence_run_once(
                    db,
                    run_id=run_id,
                    ended_at=now,
                    reason="kill_switch",
                )
                if fenced:
                    stats["runs_cancelled"] += 1
                    stats["grants_revoked"] += live_grants
            else:
                await reconcile_cancelled_run_event(db, run_id=run_id)

            task = await db.get(LabTask, task_id)
            if task is not None and task.status not in _TERMINAL_TASK_STATES:
                existing_command = await db.scalar(
                    select(LabTerminalizationCommand.command_id).where(
                        LabTerminalizationCommand.task_id == task.id,
                        LabTerminalizationCommand.hold_id == task.hold_id,
                        LabTerminalizationCommand.operation == "fail",
                    )
                )
                if existing_command is None:
                    await lab_task_service.fail_task(db, task, reason="kill_switch")
                    await db.refresh(task)
                    terminalization_requested = (
                        task.status in _TERMINAL_TASK_STATES
                        or await db.scalar(
                            select(LabTerminalizationCommand.command_id).where(
                                LabTerminalizationCommand.task_id == task.id,
                                LabTerminalizationCommand.hold_id == task.hold_id,
                                LabTerminalizationCommand.operation == "fail",
                            )
                        )
                        is not None
                    )

            if terminalization_requested:
                stats["tasks_failed"] += 1
        except Exception:  # noqa: BLE001 — isolate one bad run; the drill goes on
            logger.warning("kill_switch teardown failed for run %s", run_id, exc_info=True)
            await db.rollback()

    return stats
