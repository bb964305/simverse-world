"""Durable outbox dispatcher (recovery plan Phase 2, gap #11).

``ledger.append_event`` / ``world_revision_service`` / ``lab_artifact_service``
durably write ``OutboxEvent`` rows in the same transaction as the state they
describe, but nothing drained them in production — a post-commit publish failure
had no replay path. This module is that claimant/retry/topic router:

* **claim** — a row is eligible when ``published_at IS NULL`` and
  ``dispatch_status='pending'`` and its backoff (``next_attempt_at``) has elapsed
  and it is not currently leased (``locked_until``). Claiming is a conditional
  UPDATE + rowcount (the lease/broker CAS idiom), so two dispatchers never
  double-claim one row.
* **route** — the row's ``topic`` selects a publisher from the registry. An
  UNKNOWN topic is quarantined to ``dispatch_status='dead'`` and NEVER marked
  published (a misrouted event must not silently vanish as delivered).
* **retry** — a publish failure bumps ``attempts`` and schedules an exponential
  backoff; past ``max_attempts`` the row dead-letters. Success stamps
  ``published_at`` (idempotent: the CAS only fires while it is still NULL).

The caller owns the session/loop; ``dispatch_once`` commits per row so one bad
publisher cannot block or roll back the others. Publishers are injected so the
engine is testable with fakes; the default registry wires the real WS/Redis
sinks but is only *activated* by the Lab Runner deployment (Phase 8), not here.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, UTC
from typing import Awaitable, Callable

from sqlalchemy import and_, or_, select, update

from app.models.lab_event import OutboxEvent

logger = logging.getLogger(__name__)

Publisher = Callable[[dict], Awaitable[None]]

MAX_ATTEMPTS = 5
LEASE_S = 30
BACKOFF_BASE_S = 2
BACKOFF_CAP_S = 300

# Topics the dispatcher knows how to route. A row whose topic is absent from the
# active publisher registry is quarantined rather than dropped.
KNOWN_TOPICS = ("lab.run.enqueue", "lab_run_event", "world_changed", "lab_control", "artifact_cleanup")


async def _publish_run_enqueue(payload: dict) -> None:
    """Durable-dispatch sink for ``lab.run.enqueue``: LPUSH the run onto the Redis
    work queue. Idempotent at the consumer — the runner's queued-guard + run lease
    skip a run already picked up, so a replayed enqueue cannot double-execute."""
    from app.lab import queue as lab_queue
    run_id = (payload or {}).get("run_id")
    if run_id:
        await lab_queue.enqueue_run(run_id)


def default_publishers() -> "dict[str, Publisher]":
    """The live publisher registry the Lab Runner activates (Phase 8 deployment).

    Only ``lab.run.enqueue`` is wired: it is the one topic with a purely durable
    guarantee (re-deliver a lost enqueue) and an idempotent consumer. The
    ``lab_run_event`` / ``world_changed`` topics are deliberately NOT wired here —
    their live path is the inline WS broadcast; wiring the dispatcher to re-broadcast
    them would double-deliver until the deployment owns that de-dup topology."""
    return {"lab.run.enqueue": _publish_run_enqueue}


def backoff_seconds(attempts: int) -> float:
    """Exponential backoff for the Nth failed attempt, capped."""
    return float(min(BACKOFF_CAP_S, BACKOFF_BASE_S ** max(1, attempts)))


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def _eligible_ids(db, *, limit: int, now: datetime) -> list[int]:
    rows = (await db.execute(
        select(OutboxEvent.id)
        .where(
            OutboxEvent.published_at.is_(None),
            OutboxEvent.dispatch_status == "pending",
            or_(OutboxEvent.next_attempt_at.is_(None), OutboxEvent.next_attempt_at <= now),
            or_(OutboxEvent.locked_until.is_(None), OutboxEvent.locked_until <= now),
        )
        .order_by(OutboxEvent.id)
        .limit(limit)
    )).scalars().all()
    return list(rows)


async def _claim(db, *, outbox_id: int, now: datetime, lease_s: int) -> bool:
    """CAS-lease one row. Returns True iff this dispatcher won the claim."""
    result = await db.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.id == outbox_id,
            OutboxEvent.published_at.is_(None),
            OutboxEvent.dispatch_status == "pending",
            or_(OutboxEvent.locked_until.is_(None), OutboxEvent.locked_until <= now),
        )
        .values(locked_until=now + timedelta(seconds=lease_s))
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return (result.rowcount or 0) == 1


async def dispatch_once(
    db, *, publishers: dict[str, Publisher], limit: int = 100, lease_s: int = LEASE_S,
    max_attempts: int = MAX_ATTEMPTS, now: datetime | None = None,
) -> dict:
    """Claim, route, and settle a batch of eligible outbox rows once. Returns
    ``{"published", "retried", "dead", "quarantined", "claimed"}`` counts.

    Commits per row so a single failing publisher isolates to its own row. A
    publisher must be idempotent under a given ``event_id`` — the dispatcher's
    at-least-once contract can redeliver a row whose lease lapsed mid-publish."""
    now = _now(now)
    stats = {"published": 0, "retried": 0, "dead": 0, "quarantined": 0, "claimed": 0}

    for outbox_id in await _eligible_ids(db, limit=limit, now=now):
        if not await _claim(db, outbox_id=outbox_id, now=now, lease_s=lease_s):
            continue  # another dispatcher won it
        stats["claimed"] += 1
        row = await db.get(OutboxEvent, outbox_id)
        if row is None:  # pragma: no cover - defensive
            continue

        publisher = publishers.get(row.topic)
        if publisher is None:
            # Unknown/unroutable topic — quarantine, never mark published.
            row.dispatch_status = "dead"
            row.last_error = "unknown_topic"
            row.locked_until = None
            await db.commit()
            stats["quarantined"] += 1
            logger.warning("outbox row %s quarantined: unknown topic %r", outbox_id, row.topic)
            continue

        try:
            await publisher(row.payload_json or {})
        except Exception as exc:  # noqa: BLE001 — an untrusted sink; retry/backoff
            row.attempts = (row.attempts or 0) + 1
            row.last_error = str(exc)[:200]
            row.locked_until = None
            if row.attempts >= max_attempts:
                row.dispatch_status = "dead"
                await db.commit()
                stats["dead"] += 1
                logger.error("outbox row %s dead-lettered after %s attempts", outbox_id, row.attempts)
            else:
                row.next_attempt_at = now + timedelta(seconds=backoff_seconds(row.attempts))
                await db.commit()
                stats["retried"] += 1
            continue

        # Success — idempotent mark (CAS keeps first-write-wins for published_at).
        marked = await db.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == outbox_id, OutboxEvent.published_at.is_(None))
            .values(published_at=now, dispatch_status="published", locked_until=None)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if (marked.rowcount or 0) == 1:
            stats["published"] += 1

    return stats


async def run_dispatch_loop(session_factory, *, publishers: dict[str, Publisher],
                            interval_s: float, stop_event) -> None:
    """Long-lived drain loop for the Lab Runner (wired by Phase 8 deployment,
    not started here). Each pass opens its own session, dispatches a batch, and
    sleeps ``interval_s``. Resilient to transient DB errors."""
    import asyncio

    while not stop_event.is_set():
        try:
            async with session_factory() as db:
                await dispatch_once(db, publishers=publishers)
        except Exception:  # noqa: BLE001
            logger.warning("outbox dispatch pass failed; retrying", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
