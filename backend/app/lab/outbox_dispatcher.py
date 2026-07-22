"""Durable outbox dispatcher with trust-plane topic ownership.

The outbox is shared across multiple delivery planes, so a dispatcher instance
must only claim the topics it owns. Known-but-not-owned topics stay pending for
their proper owner; truly unknown topics are quarantined. Publishers receive the
full outbox envelope so they can make idempotency decisions on ``event_id``.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Awaitable, Callable

from sqlalchemy import not_, or_, select, update

from app.models.lab_event import OutboxEvent

logger = logging.getLogger(__name__)

Publisher = Callable[[dict], Awaitable[None]]

MAX_ATTEMPTS = 5
LEASE_S = 30
BACKOFF_BASE_S = 2
BACKOFF_CAP_S = 300

TOPIC_OWNERS = {
    "lab.run.enqueue": "lab_runner",
    "lab_run_event": "realtime_relay",
    "lab.task.terminalized": "lab_terminalizer",
    "world_changed": "world_relay",
    "lab_control": "lab_runner",
    "artifact.cleanup.requested": "lab_runner",
    "artifact.cleanup.completed": "lab_runner",
}
KNOWN_TOPICS = tuple(TOPIC_OWNERS)


async def _publish_run_enqueue(envelope: dict) -> None:
    from app.lab import queue as lab_queue

    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("lab.run.enqueue payload must be an object")
    envelope_run_id = envelope.get("run_id")
    run_id = payload.get("run_id")
    protocol_version = payload.get("protocol_version")
    if (
        not isinstance(envelope_run_id, str)
        or not envelope_run_id
        or not isinstance(run_id, str)
        or run_id != envelope_run_id
    ):
        raise ValueError("lab.run.enqueue envelope and payload run binding mismatch")
    if type(protocol_version) is not int or protocol_version not in {1, 2}:
        raise ValueError("lab.run.enqueue payload has invalid protocol_version")
    await lab_queue.enqueue_run(run_id, protocol_version=protocol_version)


async def _publish_lab_control(_envelope: dict) -> None:
    """Best-effort wakeup topic.

    The control plane still polls durable DB state on every pass, so a lost
    wakeup must not lose the underlying command. Until a dedicated wakeup bus is
    restored in this worktree, publishing is intentionally a no-op.
    """


async def _publish_artifact_cleanup(_envelope: dict) -> None:
    """Wakeup only; the Runner reconciles the durable Artifact operation rows."""


def owned_topics(owner: str) -> frozenset[str]:
    return frozenset(topic for topic, topic_owner in TOPIC_OWNERS.items() if topic_owner == owner)


def default_publishers(*, owner: str | None = None) -> dict[str, Publisher]:
    registry: dict[str, Publisher] = {
        "lab.run.enqueue": _publish_run_enqueue,
        "lab_control": _publish_lab_control,
        "artifact.cleanup.requested": _publish_artifact_cleanup,
        "artifact.cleanup.completed": _publish_artifact_cleanup,
    }
    if owner is None:
        return dict(registry)
    return {
        topic: publisher
        for topic, publisher in registry.items()
        if TOPIC_OWNERS.get(topic) == owner
    }


def backoff_seconds(attempts: int) -> float:
    return float(min(BACKOFF_CAP_S, BACKOFF_BASE_S ** max(1, attempts)))


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


def _envelope(row: OutboxEvent) -> dict:
    return {
        "outbox_id": row.id,
        "event_id": row.event_id,
        "tenant_id": row.tenant_id,
        "run_id": row.run_id,
        "topic": row.topic,
        "payload": row.payload_json or {},
    }


async def _eligible_ids(
    db,
    *,
    limit: int,
    now: datetime,
    owned: frozenset[str] | None,
) -> list[int]:
    statement = (
        select(OutboxEvent.id)
        .where(
            OutboxEvent.published_at.is_(None),
            OutboxEvent.dispatch_status == "pending",
            or_(OutboxEvent.next_attempt_at.is_(None), OutboxEvent.next_attempt_at <= now),
            or_(OutboxEvent.locked_until.is_(None), OutboxEvent.locked_until <= now),
        )
        .order_by(OutboxEvent.id)
        .limit(limit)
    )
    if owned is not None:
        statement = statement.where(
            or_(
                OutboxEvent.topic.in_(tuple(owned)),
                not_(OutboxEvent.topic.in_(KNOWN_TOPICS)),
            )
        )
    rows = (await db.execute(statement)).scalars().all()
    return list(rows)


async def _claim(db, *, outbox_id: int, now: datetime, lease_s: int) -> bool:
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
    db,
    *,
    publishers: dict[str, Publisher],
    owned_topics: frozenset[str] | None = None,
    limit: int = 100,
    lease_s: int = LEASE_S,
    max_attempts: int = MAX_ATTEMPTS,
    now: datetime | None = None,
) -> dict:
    """Claim, route, and settle one eligible batch.

    ``owned_topics`` narrows claims to this dispatcher's trust plane, except that
    unknown topics are still claimed and quarantined by whichever dispatcher sees
    them first.
    """

    now = _now(now)
    stats = {
        "published": 0,
        "retried": 0,
        "dead": 0,
        "quarantined": 0,
        "claimed": 0,
        "skipped": 0,
    }

    for outbox_id in await _eligible_ids(db, limit=limit, now=now, owned=owned_topics):
        if not await _claim(db, outbox_id=outbox_id, now=now, lease_s=lease_s):
            continue
        stats["claimed"] += 1
        row = await db.get(OutboxEvent, outbox_id)
        if row is None:
            continue

        if owned_topics is not None and row.topic in KNOWN_TOPICS and row.topic not in owned_topics:
            row.locked_until = None
            await db.commit()
            stats["skipped"] += 1
            continue

        publisher = publishers.get(row.topic)
        if publisher is None:
            if row.topic in KNOWN_TOPICS:
                row.locked_until = None
                await db.commit()
                stats["skipped"] += 1
                continue
            row.dispatch_status = "dead"
            row.last_error = "unknown_topic"
            row.locked_until = None
            await db.commit()
            stats["quarantined"] += 1
            logger.warning("outbox row %s quarantined: unknown topic %r", outbox_id, row.topic)
            continue

        try:
            envelope = _envelope(row)
            if owned_topics is None and publisher not in {_publish_run_enqueue, _publish_lab_control}:
                await publisher(row.payload_json or {})
            else:
                await publisher(envelope)
        except Exception as exc:  # noqa: BLE001
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

        marked = await db.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == outbox_id, OutboxEvent.published_at.is_(None))
            .values(
                published_at=now,
                dispatch_status="published",
                locked_until=None,
                last_error=None,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if (marked.rowcount or 0) == 1:
            stats["published"] += 1

    return stats


async def run_dispatch_loop(
    session_factory,
    *,
    publishers: dict[str, Publisher],
    owned_topics: frozenset[str] | None = None,
    interval_s: float = 1.0,
    stop_event,
) -> None:
    import asyncio

    while not stop_event.is_set():
        try:
            async with session_factory() as db:
                await dispatch_once(
                    db,
                    publishers=publishers,
                    owned_topics=owned_topics,
                )
        except Exception:  # noqa: BLE001
            logger.warning("outbox dispatch pass failed; retrying", exc_info=True)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            pass
