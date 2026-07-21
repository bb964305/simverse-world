"""Real-Postgres protocol-v2 durability regressions (Approved-v10 AC05/AC06).

The release database is disposable and migrated to head before these probes.
Normal developer runs skip when it is absent; setting LAB_POSTGRES_REQUIRED=1
turns every missing or non-Postgres prerequisite into a hard failure.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


pytestmark = pytest.mark.lab_postgres

_REQUIRED = os.environ.get("LAB_POSTGRES_REQUIRED", "").lower() in {
    "1", "true", "yes", "on",
}


def _require_or_skip(reason: str) -> None:
    if _REQUIRED:
        pytest.fail(f"LAB_POSTGRES_REQUIRED=1 but {reason}")
    pytest.skip(reason)


@pytest.fixture(scope="module")
def postgres_url() -> str:
    url = os.environ.get("LAB_TEST_DATABASE_URL", "")
    if not url:
        _require_or_skip("LAB_TEST_DATABASE_URL is absent")
    if not url.startswith(("postgresql+asyncpg://", "postgresql://")):
        _require_or_skip("LAB_TEST_DATABASE_URL is not a PostgreSQL URL")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


@pytest.fixture(scope="module")
def migrated_postgres_url(postgres_url: str) -> str:
    backend = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["DATABASE_URL"] = postgres_url
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=backend,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
    )
    if completed.returncode != 0:
        _require_or_skip(f"alembic upgrade head failed:\n{completed.stdout[-3000:]}")
    return postgres_url


@pytest.fixture
async def pg_factory(migrated_postgres_url: str):
    engine = create_async_engine(migrated_postgres_url, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            if conn.dialect.name != "postgresql":
                _require_or_skip(f"connected dialect is {conn.dialect.name!r}")
            database, disposable, revision, schema_columns = (
                await conn.execute(
                    text(
                        "SELECT current_database(), "
                        "current_setting('simverse.release_disposable', true), "
                        "(SELECT version_num FROM alembic_version), "
                        "(SELECT count(*) FROM information_schema.columns "
                        " WHERE table_schema='public' "
                        " AND table_name='lab_runtime_sessions' "
                        " AND column_name IN "
                        " ('creation_owner','creation_lease_expires_at','provider_name'))"
                    )
                )
            ).one()
            release_run_id = os.environ.get("LAB_RELEASE_RUN_ID", "")
            expected_database = f"simverse_lab_release_{release_run_id}"
            if _REQUIRED and (
                not release_run_id
                or database != expected_database
                or disposable != "on"
                or revision != "039_add_lab_protocol_v2_state"
                or schema_columns != 3
            ):
                pytest.fail(
                    "runtime-v2 PG tests require the exact disposable 039 schema: "
                    f"database={database!r}, expected={expected_database!r}, "
                    f"disposable={disposable!r}, revision={revision!r}, "
                    f"schema_columns={schema_columns}"
                )
        yield async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
    finally:
        await engine.dispose()


async def _insert_run(
    factory,
    run_id: str,
    *,
    protocol_version: int = 2,
    lease_epoch: int | None = None,
    lease_owner: str = "runner-owner",
) -> None:
    async with factory() as db:
        await db.execute(
            text(
                "INSERT INTO lab_runs "
                "(id, task_id, researcher_slug, adapter, status, protocol_version, "
                "created_at) VALUES "
                "(:id, :task, 'sage', 'simverse_ref', 'queued', :version, "
                "clock_timestamp())"
            ),
            {"id": run_id, "task": f"task-{run_id}", "version": protocol_version},
        )
        if lease_epoch is not None:
            await db.execute(
                text(
                    "INSERT INTO lab_run_leases "
                    "(run_id, owner_id, fencing_epoch, heartbeat_at, expires_at, "
                    "created_at, updated_at) VALUES "
                    "(:run_id, :owner, :epoch, clock_timestamp(), "
                    "clock_timestamp() + interval '5 minutes', clock_timestamp(), "
                    "clock_timestamp())"
                ),
                {"run_id": run_id, "owner": lease_owner, "epoch": lease_epoch},
            )
        await db.commit()


@pytest.mark.anyio
async def test_raw_sql_cannot_mutate_protocol_version_after_creation(pg_factory):
    """ORM guards are insufficient: legacy binaries can issue direct UPDATEs."""
    run_id = f"immutable-{uuid.uuid4()}"
    await _insert_run(pg_factory, run_id)

    with pytest.raises(DBAPIError):
        async with pg_factory() as db:
            await db.execute(
                text("UPDATE lab_runs SET protocol_version = 1 WHERE id = :id"),
                {"id": run_id},
            )
            await db.commit()

    async with pg_factory() as db:
        actual = await db.scalar(
            text("SELECT protocol_version FROM lab_runs WHERE id = :id"),
            {"id": run_id},
        )
    assert actual == 2


class CrashAfterProviderCreate(RuntimeError):
    pass


class RecordingProvider:
    """Provider that creates once, loses the response, then reattaches by client id."""

    def __init__(self, factory, *, host_lost: bool = False) -> None:
        self.name = "recording-provider"
        self.factory = factory
        self.host_lost = host_lost
        self.handshake_calls = 0
        self.create_calls: list[tuple[str, int]] = []
        self.reattach_calls: list[tuple[str, int]] = []
        self.locators: dict[tuple[str, int], str] = {}
        self.registration_seen_before_create = False

    async def handshake(self):
        self.handshake_calls += 1
        return {
            "schema_version": 2,
            "protocol_version": 2,
            "provider_name": self.name,
            "durability_class": "session_affine",
            "reattach_capability": "client_run_id",
            "effect_mode": "broker_only",
            "capabilities": ["broker_mediation"],
        }

    @staticmethod
    def _binding(args, kwargs) -> tuple[str, int]:
        client_run_id = kwargs.get("client_run_id")
        epoch = kwargs.get("epoch")
        if client_run_id is None and args:
            client_run_id = args[0]
        if epoch is None and len(args) > 1:
            epoch = args[1]
        return str(client_run_id), int(epoch)

    async def create_session(self, *args, **kwargs):
        client_run_id, epoch = self._binding(args, kwargs)
        self.create_calls.append((client_run_id, epoch))
        async with self.factory() as observer:
            row = (
                await observer.execute(
                    text(
                        "SELECT status, client_run_id, fencing_epoch "
                        "FROM lab_runtime_sessions "
                        "WHERE client_run_id = :client AND fencing_epoch = :epoch"
                    ),
                    {"client": client_run_id, "epoch": epoch},
                )
            ).mappings().one_or_none()
        self.registration_seen_before_create = bool(
            row and row["status"] == "creating"
        )
        locator = self.locators.setdefault(
            (client_run_id, epoch), f"provider-session-{uuid.uuid4()}"
        )
        raise CrashAfterProviderCreate(locator)

    # Accept either conventional provider spelling without weakening behavior.
    create = create_session

    async def reattach_session(self, *args, **kwargs):
        client_run_id, epoch = self._binding(args, kwargs)
        self.reattach_calls.append((client_run_id, epoch))
        if self.host_lost:
            return None
        locator = self.locators[(client_run_id, epoch)]
        return {
            "locator": locator,
            "session_id": locator,
            "durability_class": "session_affine",
        }

    reattach = reattach_session


def _runtime_sessions_api():
    try:
        from app.lab import runtime_sessions
    except ImportError as exc:  # expected-red on 77b64c2
        raise AssertionError(
            "app.lab.runtime_sessions must own durable provider registration "
            "and create/reattach"
        ) from exc
    return runtime_sessions


@pytest.mark.anyio
async def test_provider_create_crash_reattaches_same_registered_session(pg_factory):
    runtime_sessions = _runtime_sessions_api()
    run_id = f"rt-{uuid.uuid4().hex}"
    await _insert_run(pg_factory, run_id, lease_epoch=9)
    provider = RecordingProvider(pg_factory)

    async with pg_factory() as db:
        with pytest.raises(CrashAfterProviderCreate):
            await runtime_sessions.create_or_reattach(
                db, run_id=run_id, epoch=9, owner_id="runner-owner", provider=provider,
                durability_class="session_affine",
            )
    assert provider.registration_seen_before_create
    assert len(provider.create_calls) == 1

    async with pg_factory() as db:
        creating = (
            await db.execute(
                text(
                    "SELECT client_run_id, fencing_epoch, status "
                    "FROM lab_runtime_sessions WHERE run_id = :run"
                ),
                {"run": run_id},
            )
        ).mappings().one()
    assert creating["client_run_id"]
    assert creating["fencing_epoch"] == 9
    assert creating["status"] == "creating"

    async with pg_factory() as db:
        ready = await runtime_sessions.create_or_reattach(
            db, run_id=run_id, epoch=9, owner_id="runner-owner", provider=provider,
            durability_class="session_affine",
        )
    assert ready.status == "ready"
    assert ready.client_run_id == creating["client_run_id"]
    assert len(provider.create_calls) == 1
    assert provider.reattach_calls == [(creating["client_run_id"], 9)]

    # A ready retry re-verifies the same provider binding and cannot create again.
    async with pg_factory() as db:
        same = await runtime_sessions.create_or_reattach(
            db, run_id=run_id, epoch=9, owner_id="runner-owner", provider=provider,
            durability_class="session_affine",
        )
    assert same.id == ready.id
    assert len(provider.create_calls) == 1
    assert len(provider.reattach_calls) == 2


@pytest.mark.anyio
async def test_session_affine_host_loss_quarantines_instead_of_recreating(pg_factory):
    runtime_sessions = _runtime_sessions_api()
    run_id = f"hl-{uuid.uuid4().hex}"
    await _insert_run(pg_factory, run_id, lease_epoch=4)
    provider = RecordingProvider(pg_factory, host_lost=True)

    async with pg_factory() as db:
        with pytest.raises(CrashAfterProviderCreate):
            await runtime_sessions.create_or_reattach(
                db, run_id=run_id, epoch=4, owner_id="runner-owner", provider=provider,
                durability_class="session_affine",
            )
    async with pg_factory() as db:
        try:
            await runtime_sessions.create_or_reattach(
                db, run_id=run_id, epoch=4, owner_id="runner-owner", provider=provider,
                durability_class="session_affine",
            )
        except Exception:
            # Fail-closed callers may surface a quarantine exception; the DB
            # state below remains the authoritative recovery contract.
            pass

    async with pg_factory() as db:
        status = await db.scalar(
            text("SELECT status FROM lab_runtime_sessions WHERE run_id = :run"),
            {"run": run_id},
        )
    assert status == "quarantined"
    assert len(provider.create_calls) == 1
    assert len(provider.reattach_calls) == 1


class ConcurrentProvider:
    name = "concurrent-provider"

    def __init__(self) -> None:
        self.create_started = asyncio.Event()
        self.release_create = asyncio.Event()
        self.create_calls = 0
        self.reattach_calls = 0
        self.locator: str | None = None

    async def handshake(self):
        return {
            "schema_version": 2,
            "protocol_version": 2,
            "provider_name": self.name,
            "durability_class": "session_affine",
            "reattach_capability": "client_run_id",
            "effect_mode": "broker_only",
            "capabilities": ["broker_mediation"],
        }

    async def create_session(self, *, client_run_id: str, epoch: int):
        self.create_calls += 1
        self.locator = f"provider:{client_run_id}:{epoch}"
        self.create_started.set()
        await self.release_create.wait()
        return {
            "locator": self.locator,
            "session_id": self.locator,
            "durability_class": "session_affine",
        }

    async def reattach_session(self, *, client_run_id: str, epoch: int):
        self.reattach_calls += 1
        assert self.release_create.is_set(), "reattach raced a live create owner"
        return {
            "locator": self.locator,
            "session_id": self.locator,
            "durability_class": "session_affine",
        }


@pytest.mark.anyio
async def test_concurrent_create_waits_for_live_owner_instead_of_quarantining(pg_factory):
    runtime_sessions = _runtime_sessions_api()
    run_id = f"cc-{uuid.uuid4().hex}"
    await _insert_run(pg_factory, run_id, lease_epoch=12)
    provider = ConcurrentProvider()

    async def open_session():
        async with pg_factory() as db:
            return await runtime_sessions.create_or_reattach(
                db,
                run_id=run_id,
                epoch=12,
                owner_id="runner-owner",
                provider=provider,
                durability_class="session_affine",
            )

    first = asyncio.create_task(open_session())
    await asyncio.wait_for(provider.create_started.wait(), timeout=2)
    second = asyncio.create_task(open_session())
    await asyncio.sleep(0.1)
    assert not second.done()
    assert provider.reattach_calls == 0

    provider.release_create.set()
    first_ready, second_ready = await asyncio.gather(first, second)
    assert first_ready.id == second_ready.id
    assert first_ready.status == second_ready.status == "ready"
    assert provider.create_calls == 1
    assert provider.reattach_calls == 1


@pytest.mark.anyio
async def test_final_lease_lock_rechecks_expiry_after_row_lock_wait(pg_factory):
    runtime_sessions = _runtime_sessions_api()
    run_id = f"ex-{uuid.uuid4().hex}"
    await _insert_run(pg_factory, run_id, lease_epoch=0, lease_owner="owner-a")
    provider = ConcurrentProvider()

    async def open_session():
        async with pg_factory() as db:
            return await runtime_sessions.create_or_reattach(
                db,
                run_id=run_id,
                epoch=0,
                owner_id="owner-a",
                provider=provider,
                durability_class="session_affine",
            )

    opening = asyncio.create_task(open_session())
    await asyncio.wait_for(provider.create_started.wait(), timeout=2)
    async with pg_factory() as db:
        await db.execute(
            text(
                "UPDATE lab_run_leases SET "
                "expires_at = clock_timestamp() + interval '0.5 seconds' "
                "WHERE run_id = :run"
            ),
            {"run": run_id},
        )
        await db.commit()

    async with pg_factory() as holder:
        await holder.execute(
            text("SELECT run_id FROM lab_run_leases WHERE run_id = :run FOR UPDATE"),
            {"run": run_id},
        )
        provider.release_create.set()
        await asyncio.sleep(0.8)
        assert not opening.done(), "final ready did not wait for the lease row lock"
        await holder.rollback()

    with pytest.raises(runtime_sessions.RuntimeSessionError, match="live lease"):
        await asyncio.wait_for(opening, timeout=2)
    async with pg_factory() as db:
        status = await db.scalar(
            text("SELECT status FROM lab_runtime_sessions WHERE run_id = :run"),
            {"run": run_id},
        )
    assert status == "quarantined"


@pytest.mark.anyio
async def test_stale_or_wrong_lease_owner_is_rejected_before_provider_effect(pg_factory):
    runtime_sessions = _runtime_sessions_api()
    run_id = f"sl-{uuid.uuid4().hex}"
    await _insert_run(
        pg_factory, run_id, lease_epoch=3, lease_owner="current-owner"
    )
    provider = RecordingProvider(pg_factory)

    async with pg_factory() as db:
        with pytest.raises(runtime_sessions.RuntimeSessionError, match="live lease"):
            await runtime_sessions.create_or_reattach(
                db,
                run_id=run_id,
                epoch=1,
                owner_id="stale-owner",
                provider=provider,
                durability_class="session_affine",
            )

    assert provider.handshake_calls == 0
    assert provider.create_calls == []
    assert provider.reattach_calls == []
    async with pg_factory() as db:
        count = await db.scalar(
            text("SELECT count(*) FROM lab_runtime_sessions WHERE run_id = :run"),
            {"run": run_id},
        )
    assert count == 0


class BlockingHandshakeProvider:
    name = "blocking-handshake"

    def __init__(self) -> None:
        self.handshake_started = asyncio.Event()
        self.release_handshake = asyncio.Event()
        self.create_calls = 0
        self.reattach_calls = 0

    async def handshake(self):
        self.handshake_started.set()
        await self.release_handshake.wait()
        return {
            "schema_version": 2,
            "protocol_version": 2,
            "provider_name": self.name,
            "durability_class": "session_affine",
            "reattach_capability": "client_run_id",
            "effect_mode": "broker_only",
            "capabilities": ["broker_mediation"],
        }

    async def create_session(self, **_kwargs):
        self.create_calls += 1
        raise AssertionError("fenced owner must not create a provider session")

    async def reattach_session(self, **_kwargs):
        self.reattach_calls += 1
        raise AssertionError("fenced owner must not reattach a provider session")


@pytest.mark.anyio
async def test_takeover_during_handshake_cannot_leave_stale_registration(pg_factory):
    runtime_sessions = _runtime_sessions_api()
    run_id = f"th-{uuid.uuid4().hex}"
    await _insert_run(pg_factory, run_id, lease_epoch=0, lease_owner="owner-a")
    provider = BlockingHandshakeProvider()

    async def open_session():
        async with pg_factory() as db:
            return await runtime_sessions.create_or_reattach(
                db,
                run_id=run_id,
                epoch=0,
                owner_id="owner-a",
                provider=provider,
                durability_class="session_affine",
            )

    opening = asyncio.create_task(open_session())
    await asyncio.wait_for(provider.handshake_started.wait(), timeout=2)
    async with pg_factory() as db:
        await db.execute(
            text(
                "UPDATE lab_run_leases SET owner_id = 'owner-b', fencing_epoch = 1, "
                "heartbeat_at = clock_timestamp(), "
                "expires_at = clock_timestamp() + interval '5 minutes' "
                "WHERE run_id = :run"
            ),
            {"run": run_id},
        )
        await db.commit()
    provider.release_handshake.set()

    with pytest.raises(runtime_sessions.RuntimeSessionError, match="live lease"):
        await opening
    async with pg_factory() as db:
        count = await db.scalar(
            text("SELECT count(*) FROM lab_runtime_sessions WHERE run_id = :run"),
            {"run": run_id},
        )
    assert count == 0
    assert provider.create_calls == 0
    assert provider.reattach_calls == 0


class BlockingReadyProbeProvider:
    name = "blocking-ready-probe"

    def __init__(self) -> None:
        self.probe_started = asyncio.Event()
        self.release_probe = asyncio.Event()
        self.create_returned = asyncio.Event()
        self.create_calls = 0
        self.reattach_calls = 0

    async def handshake(self):
        return {
            "schema_version": 2,
            "protocol_version": 2,
            "provider_name": self.name,
            "durability_class": "session_affine",
            "reattach_capability": "client_run_id",
            "effect_mode": "broker_only",
            "capabilities": ["broker_mediation"],
        }

    @staticmethod
    def _result(client_run_id: str):
        locator = f"provider:{client_run_id}"
        return {
            "locator": locator,
            "session_id": locator,
            "durability_class": "session_affine",
        }

    async def create_session(self, *, client_run_id: str, epoch: int):
        self.create_calls += 1
        self.create_returned.set()
        return self._result(client_run_id)

    async def reattach_session(self, *, client_run_id: str, epoch: int):
        self.reattach_calls += 1
        self.probe_started.set()
        await self.release_probe.wait()
        return self._result(client_run_id)


@pytest.mark.anyio
async def test_ready_probe_cannot_return_stale_ready_after_concurrent_fence(pg_factory):
    runtime_sessions = _runtime_sessions_api()
    run_id = f"rf-{uuid.uuid4().hex}"
    await _insert_run(pg_factory, run_id, lease_epoch=0, lease_owner="owner-a")
    provider = BlockingReadyProbeProvider()

    async with pg_factory() as db:
        ready = await runtime_sessions.create_or_reattach(
            db,
            run_id=run_id,
            epoch=0,
            owner_id="owner-a",
            provider=provider,
            durability_class="session_affine",
        )
    assert ready.status == "ready"

    async def retry_ready():
        async with pg_factory() as db:
            return await runtime_sessions.create_or_reattach(
                db,
                run_id=run_id,
                epoch=0,
                owner_id="owner-a",
                provider=provider,
                durability_class="session_affine",
            )

    retry = asyncio.create_task(retry_ready())
    await asyncio.wait_for(provider.probe_started.wait(), timeout=2)
    async with pg_factory() as db:
        await db.execute(
            text(
                "UPDATE lab_run_leases SET owner_id = 'owner-b', fencing_epoch = 1, "
                "heartbeat_at = clock_timestamp(), "
                "expires_at = clock_timestamp() + interval '5 minutes' "
                "WHERE run_id = :run"
            ),
            {"run": run_id},
        )
        await db.execute(
            text(
                "UPDATE lab_runtime_sessions SET status = 'fenced' "
                "WHERE run_id = :run AND status = 'ready'"
            ),
            {"run": run_id},
        )
        await db.commit()
    provider.release_probe.set()

    with pytest.raises(runtime_sessions.RuntimeSessionError, match="fenced|live lease"):
        await retry
    async with pg_factory() as db:
        status = await db.scalar(
            text("SELECT status FROM lab_runtime_sessions WHERE run_id = :run"),
            {"run": run_id},
        )
    assert status == "fenced"
    assert provider.create_calls == 1
    assert provider.reattach_calls == 1


@pytest.mark.anyio
async def test_final_ready_cas_locks_lease_against_snapshot_takeover(
    pg_factory, migrated_postgres_url
):
    runtime_sessions = _runtime_sessions_api()
    run_id = f"lc-{uuid.uuid4().hex}"
    await _insert_run(pg_factory, run_id, lease_epoch=0, lease_owner="owner-a")
    provider = BlockingReadyProbeProvider()
    suffix = uuid.uuid4().hex
    function_name = f"block_runtime_ready_{suffix}"
    trigger_name = f"block_runtime_ready_{suffix}"
    lock_key = int(suffix[:7], 16)
    engine = create_async_engine(migrated_postgres_url, pool_pre_ping=True)
    control = await engine.connect()
    opening = None
    takeover = None
    lock_held = False
    try:
        async with control.begin():
            await control.execute(
                text(
                    f"CREATE FUNCTION public.{function_name}() RETURNS trigger "
                    "LANGUAGE plpgsql AS $body$ BEGIN "
                    "IF OLD.status = 'creating' AND NEW.status = 'ready' THEN "
                    f"PERFORM pg_advisory_xact_lock({lock_key}); "
                    "END IF; RETURN NEW; END $body$"
                )
            )
            await control.execute(
                text(
                    f"CREATE TRIGGER {trigger_name} BEFORE UPDATE ON "
                    "lab_runtime_sessions FOR EACH ROW EXECUTE FUNCTION "
                    f"public.{function_name}()"
                )
            )
        await control.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
        await control.commit()
        lock_held = True

        async def open_session():
            async with pg_factory() as db:
                return await runtime_sessions.create_or_reattach(
                    db,
                    run_id=run_id,
                    epoch=0,
                    owner_id="owner-a",
                    provider=provider,
                    durability_class="session_affine",
                )

        opening = asyncio.create_task(open_session())
        await asyncio.wait_for(provider.create_returned.wait(), timeout=2)
        for _ in range(200):
            async with pg_factory() as db:
                waiting = await db.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks "
                        "WHERE locktype = 'advisory' AND NOT granted AND objid = :key"
                    ),
                    {"key": lock_key},
                )
            if waiting:
                break
            await asyncio.sleep(0.01)
        assert waiting == 1
        assert not opening.done()

        async def take_over_lease():
            async with pg_factory() as db:
                await db.execute(
                    text(
                        "UPDATE lab_run_leases SET owner_id = 'owner-b', "
                        "fencing_epoch = 1, heartbeat_at = clock_timestamp(), "
                        "expires_at = clock_timestamp() + interval '5 minutes' "
                        "WHERE run_id = :run"
                    ),
                    {"run": run_id},
                )
                await db.commit()

        takeover = asyncio.create_task(take_over_lease())
        await asyncio.sleep(0.1)
        assert not takeover.done(), "takeover bypassed the final ready lease lock"

        await control.execute(
            text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
        )
        await control.commit()
        lock_held = False
        ready = await asyncio.wait_for(opening, timeout=2)
        await asyncio.wait_for(takeover, timeout=2)
        assert ready.status == "ready"
    finally:
        if lock_held:
            await control.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
            )
            await control.commit()
        for task in (opening, takeover):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        async with control.begin():
            await control.execute(
                text(f"DROP TRIGGER IF EXISTS {trigger_name} ON lab_runtime_sessions")
            )
            await control.execute(
                text(f"DROP FUNCTION IF EXISTS public.{function_name}()")
            )
        await control.close()
        await engine.dispose()


@pytest.mark.anyio
async def test_final_session_updates_recheck_expiry_and_release_lease_locks(
    pg_factory, migrated_postgres_url
):
    runtime_sessions = _runtime_sessions_api()
    mark_run_id = f"me-{uuid.uuid4().hex}"
    verify_run_id = f"ve-{uuid.uuid4().hex}"
    await _insert_run(pg_factory, mark_run_id, lease_epoch=0, lease_owner="owner-a")
    await _insert_run(
        pg_factory, verify_run_id, lease_epoch=0, lease_owner="owner-a"
    )
    mark_provider = BlockingReadyProbeProvider()
    verify_provider = BlockingReadyProbeProvider()

    async with pg_factory() as db:
        ready = await runtime_sessions.create_or_reattach(
            db,
            run_id=verify_run_id,
            epoch=0,
            owner_id="owner-a",
            provider=verify_provider,
            durability_class="session_affine",
        )
    assert ready.status == "ready"

    suffix = uuid.uuid4().hex
    function_name = f"delay_runtime_transition_{suffix}"
    trigger_name = f"delay_runtime_transition_{suffix}"
    lock_key = int(suffix[:7], 16)
    engine = create_async_engine(migrated_postgres_url, pool_pre_ping=True)
    control = await engine.connect()
    mark_opening = None
    verify_opening = None
    lock_held = False

    async def wait_until_transition_blocks() -> None:
        waiting = 0
        for _ in range(200):
            async with pg_factory() as db:
                waiting = await db.scalar(
                    text(
                        "SELECT count(*) FROM pg_locks "
                        "WHERE locktype = 'advisory' AND NOT granted AND objid = :key"
                    ),
                    {"key": lock_key},
                )
            if waiting:
                break
            await asyncio.sleep(0.01)
        assert waiting == 1

    async def open_mark_session():
        async with pg_factory() as db:
            return await runtime_sessions.create_or_reattach(
                db,
                run_id=mark_run_id,
                epoch=0,
                owner_id="owner-a",
                provider=mark_provider,
                durability_class="session_affine",
            )

    async def verify_ready_session():
        async with pg_factory() as db:
            return await runtime_sessions.create_or_reattach(
                db,
                run_id=verify_run_id,
                epoch=0,
                owner_id="owner-a",
                provider=verify_provider,
                durability_class="session_affine",
            )

    try:
        async with control.begin():
            await control.execute(
                text(
                    f"CREATE FUNCTION public.{function_name}() RETURNS trigger "
                    "LANGUAGE plpgsql AS $body$ BEGIN "
                    "IF (OLD.status = 'creating' AND NEW.status = 'ready') "
                    "OR (OLD.status = 'ready' AND NEW.status = 'ready') THEN "
                    f"PERFORM pg_advisory_xact_lock({lock_key}); "
                    "END IF; RETURN NEW; END $body$"
                )
            )
            await control.execute(
                text(
                    f"CREATE TRIGGER {trigger_name} BEFORE UPDATE ON "
                    "lab_runtime_sessions FOR EACH ROW EXECUTE FUNCTION "
                    f"public.{function_name}()"
                )
            )
            await control.execute(
                text(
                    "UPDATE lab_run_leases SET "
                    "expires_at = clock_timestamp() + interval '0.5 seconds' "
                    "WHERE run_id IN (:mark_run, :verify_run)"
                ),
                {"mark_run": mark_run_id, "verify_run": verify_run_id},
            )

        await control.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
        await control.commit()
        lock_held = True

        mark_opening = asyncio.create_task(open_mark_session())
        await asyncio.wait_for(mark_provider.create_returned.wait(), timeout=2)
        await wait_until_transition_blocks()
        await asyncio.sleep(0.8)
        await control.execute(
            text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
        )
        await control.commit()
        lock_held = False
        with pytest.raises(runtime_sessions.RuntimeSessionError, match="live lease"):
            await asyncio.wait_for(mark_opening, timeout=2)

        async with pg_factory() as db:
            mark_status = await db.scalar(
                text(
                    "SELECT status FROM lab_runtime_sessions WHERE run_id = :run"
                ),
                {"run": mark_run_id},
            )
        assert mark_status == "quarantined"

        async with control.begin():
            await control.execute(
                text(
                    "UPDATE lab_run_leases SET "
                    "expires_at = clock_timestamp() + interval '0.5 seconds' "
                    "WHERE run_id = :run"
                ),
                {"run": verify_run_id},
            )
        await control.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
        await control.commit()
        lock_held = True
        verify_provider.release_probe.set()
        verify_opening = asyncio.create_task(verify_ready_session())
        await asyncio.wait_for(verify_provider.probe_started.wait(), timeout=2)
        await wait_until_transition_blocks()
        await asyncio.sleep(0.8)
        await control.execute(
            text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
        )
        await control.commit()
        lock_held = False
        with pytest.raises(runtime_sessions.RuntimeSessionError, match="live lease"):
            await asyncio.wait_for(verify_opening, timeout=2)

        async with pg_factory() as db:
            verify_status = await db.scalar(
                text(
                    "SELECT status FROM lab_runtime_sessions WHERE run_id = :run"
                ),
                {"run": verify_run_id},
            )
            await db.execute(text("SET LOCAL lock_timeout = '500ms'"))
            taken_over = await db.execute(
                text(
                    "UPDATE lab_run_leases SET owner_id = 'owner-b', "
                    "fencing_epoch = fencing_epoch + 1, "
                    "heartbeat_at = clock_timestamp(), "
                    "expires_at = clock_timestamp() + interval '5 minutes' "
                    "WHERE run_id IN (:mark_run, :verify_run) "
                    "AND expires_at <= clock_timestamp()"
                ),
                {"mark_run": mark_run_id, "verify_run": verify_run_id},
            )
            await db.commit()
        assert verify_status == "ready"
        assert taken_over.rowcount == 2
    finally:
        if lock_held:
            await control.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key}
            )
            await control.commit()
        for task in (mark_opening, verify_opening):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        async with control.begin():
            await control.execute(
                text(
                    f"DROP TRIGGER IF EXISTS {trigger_name} ON lab_runtime_sessions"
                )
            )
            await control.execute(
                text(f"DROP FUNCTION IF EXISTS public.{function_name}()")
            )
        await control.close()
        await engine.dispose()
