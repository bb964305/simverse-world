"""AC11: required real-PostgreSQL + real-Redis delivery recovery evidence."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest
import redis.asyncio as redis_async
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.lab_event import OutboxEvent
from app.models.lab_run import LabRun
from app.models.user import User


pytestmark = [pytest.mark.lab_postgres, pytest.mark.lab_redis, pytest.mark.anyio]
_TRUE = {"1", "true", "yes", "on"}


def _required_delivery_env() -> tuple[str, str, str]:
    postgres_required = os.environ.get("LAB_POSTGRES_REQUIRED", "").lower() in _TRUE
    redis_required = os.environ.get("LAB_REDIS_REQUIRED", "").lower() in _TRUE
    if not postgres_required and not redis_required:
        pytest.skip("AC11 real Postgres/Redis evidence was not requested")
    if not postgres_required or not redis_required:
        pytest.fail("AC11 requires LAB_POSTGRES_REQUIRED and LAB_REDIS_REQUIRED together")
    missing = [
        name
        for name in (
            "LAB_TEST_DATABASE_URL",
            "LAB_TEST_REDIS_URL",
            "LAB_RELEASE_RUN_ID",
            "LAB_REDIS_DISPOSABLE_TOKEN",
        )
        if not os.environ.get(name)
    ]
    if missing:
        pytest.fail("required AC11 environment is incomplete: " + ", ".join(missing))
    database_url = os.environ["LAB_TEST_DATABASE_URL"]
    if make_url(database_url).drivername != "postgresql+asyncpg":
        pytest.fail("LAB_TEST_DATABASE_URL must use postgresql+asyncpg")
    redis_url = os.environ["LAB_TEST_REDIS_URL"]
    if make_url(redis_url).drivername not in {"redis", "rediss"}:
        pytest.fail("LAB_TEST_REDIS_URL must use redis:// or rediss://")
    return database_url, redis_url, os.environ["LAB_RELEASE_RUN_ID"]


@pytest.fixture
async def delivery_factory():
    database_url, redis_url, release_run_id = _required_delivery_env()
    import app.models.lab_control  # noqa: F401 - register the P4 tables

    schema = f"lab_delivery_{uuid.uuid4().hex}"
    admin_engine = create_async_engine(database_url)
    test_engine = None
    redis = redis_async.from_url(redis_url, decode_responses=True)
    prefix = f"lab-release:{release_run_id}:{uuid.uuid4().hex}"
    try:
        async with admin_engine.begin() as connection:
            database, disposable = (
                await connection.execute(
                    text(
                        "SELECT current_database(), "
                        "current_setting('simverse.release_disposable', true)"
                    )
                )
            ).one()
            if database != f"simverse_lab_release_{release_run_id}" or disposable != "on":
                pytest.fail("AC11 database is not the asserted disposable release database")
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        test_engine = create_async_engine(
            database_url,
            connect_args={"server_settings": {"search_path": f'"{schema}"'}},
        )
        async with test_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        if await redis.ping() is not True:
            pytest.fail("AC11 Redis ping did not return true")
        yield (
            async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False),
            redis,
            prefix,
        )
    finally:
        await redis.delete(f"{prefix}:effects")
        await redis.aclose()
        if test_engine is not None:
            await test_engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin_engine.dispose()


async def _seed_control_run(factory, run_id: str) -> User:
    from app.lab import control_plane

    async with factory() as db:
        admin = User(
            id="delivery-admin",
            name="Delivery admin",
            email="delivery-admin@test.invalid",
            is_admin=True,
        )
        db.add(admin)
        db.add(
            LabRun(
                id=run_id,
                task_id="delivery-task",
                researcher_slug="sage",
                adapter="hermes",
                protocol_version="v2",
                status="running",
                scopes_json=[],
            )
        )
        await db.flush()
        await control_plane.register_runtime_target(
            db,
            run_id=run_id,
            session_id="delivery-session",
            locator={"handle": "runtime-delivery"},
            epoch=4,
        )
        await db.commit()
        return admin


async def test_db_cancel_survives_lost_redis_wakeup_and_fresh_runner_poll(delivery_factory):
    from app.config import settings
    from app.lab import control_plane
    from app.models.lab_control import LabRunControlRequest
    from app.redis_client import set_redis
    from app.routers.admin.lab import cancel_run

    factory, _redis, _prefix = delivery_factory
    admin = await _seed_control_run(factory, "delivery-run")
    settings.lab_agent_v2_enabled = True

    class RedisDown:
        def __getattr__(self, name):
            async def fail(*args, **kwargs):
                raise ConnectionError("injected wakeup loss")

            return fail

    set_redis(RedisDown())
    try:
        async with factory() as db:
            response = await cancel_run("delivery-run", admin=admin, db=db)
    finally:
        set_redis(None)

    calls = 0

    async def runtime(command):
        nonlocal calls
        calls += 1
        return {"status": "confirmed_stopped", "receipt_id": "runtime-stop-1"}

    async with factory() as restarted_db:
        stats = await control_plane.process_pending_controls(
            restarted_db,
            owner_id="fresh-runner",
            controllers={"runtime": runtime},
            now=datetime.now(UTC),
        )
    async with factory() as db:
        request = await db.get(LabRunControlRequest, response["control_request_id"])

    assert stats["completed"] == 1
    assert request.status == "completed"
    assert request.provider_stopped_at is not None
    assert calls == 1


async def test_two_postgres_dispatchers_produce_one_redis_sink_effect(delivery_factory):
    from app.lab import outbox_dispatcher

    factory, redis, prefix = delivery_factory
    async with factory() as db:
        db.add(
            OutboxEvent(
                event_id="delivery-event",
                tenant_id="tenant",
                run_id="delivery-run",
                topic="lab_control",
                payload_json={"request_id": "request-1"},
            )
        )
        await db.commit()

    async def publish(envelope):
        await redis.hincrby(f"{prefix}:effects", envelope["event_id"], 1)

    async def drain_once():
        async with factory() as db:
            return await outbox_dispatcher.dispatch_once(
                db,
                publishers={"lab_control": publish},
                owned_topics=frozenset({"lab_control"}),
            )

    first, second = await asyncio.gather(drain_once(), drain_once())
    effect_count = int(await redis.hget(f"{prefix}:effects", "delivery-event") or 0)

    assert first["published"] + second["published"] == 1
    assert effect_count == 1
