"""AC13: trust-plane topic ownership against real Postgres and Redis."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.models.lab_event import OutboxEvent
from tests.integration.test_lab_delivery_recovery import delivery_factory


pytestmark = [pytest.mark.lab_postgres, pytest.mark.lab_redis, pytest.mark.anyio]


async def test_owner_dispatchers_cannot_cross_claim_and_unknown_is_never_published(
    delivery_factory,
):
    from app.lab import outbox_dispatcher

    factory, redis, prefix = delivery_factory
    async with factory() as db:
        for index, topic in enumerate(outbox_dispatcher.KNOWN_TOPICS):
            db.add(
                OutboxEvent(
                    event_id=f"owner-{index}",
                    tenant_id="tenant",
                    run_id="run" if topic != "world_changed" else None,
                    topic=topic,
                    payload_json={"topic": topic},
                )
            )
        db.add(
            OutboxEvent(
                event_id="owner-unknown",
                tenant_id="tenant",
                topic="not_in_registry",
                payload_json={},
            )
        )
        await db.commit()

    async def publisher(envelope):
        await redis.hincrby(f"{prefix}:effects", envelope["event_id"], 1)

    async def drain(owner):
        topics = outbox_dispatcher.owned_topics(owner)
        async with factory() as db:
            return await outbox_dispatcher.dispatch_once(
                db,
                publishers={topic: publisher for topic in topics},
                owned_topics=topics,
            )

    stats = await asyncio.gather(
        *(drain(owner) for owner in sorted(set(outbox_dispatcher.TOPIC_OWNERS.values())))
    )

    async with factory() as db:
        rows = (await db.execute(select(OutboxEvent))).scalars().all()
    by_topic = {row.topic: row for row in rows}

    assert set(outbox_dispatcher.TOPIC_OWNERS) == set(outbox_dispatcher.KNOWN_TOPICS)
    assert sum(item["published"] for item in stats) == len(outbox_dispatcher.KNOWN_TOPICS)
    for topic in outbox_dispatcher.KNOWN_TOPICS:
        row = by_topic[topic]
        assert row.dispatch_status == "published"
        assert row.published_at is not None
        assert int(await redis.hget(f"{prefix}:effects", row.event_id) or 0) == 1

    unknown = by_topic["not_in_registry"]
    assert unknown.dispatch_status == "dead"
    assert unknown.published_at is None
    assert unknown.last_error == "unknown_topic"


async def test_known_topic_without_its_owner_process_remains_pending(delivery_factory):
    from app.lab import outbox_dispatcher

    factory, _redis, _prefix = delivery_factory
    async with factory() as db:
        db.add(
            OutboxEvent(
                event_id="known-without-owner",
                tenant_id="tenant",
                topic="world_changed",
                payload_json={},
            )
        )
        await db.commit()

    async with factory() as db:
        await outbox_dispatcher.dispatch_once(
            db,
            publishers=outbox_dispatcher.default_publishers(owner="lab_runner"),
            owned_topics=outbox_dispatcher.owned_topics("lab_runner"),
        )
    async with factory() as db:
        row = (
            await db.execute(
                select(OutboxEvent).where(OutboxEvent.event_id == "known-without-owner")
            )
        ).scalar_one()

    assert row.dispatch_status == "pending"
    assert row.published_at is None
    assert row.attempts == 0
    assert row.locked_until is None
