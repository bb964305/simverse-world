"""Real-Postgres protocol-v2 durability regressions (Approved-v10 AC05/AC06).

The release database is disposable and migrated to head before these probes.
Normal developer runs skip when it is absent; setting LAB_POSTGRES_REQUIRED=1
turns every missing or non-Postgres prerequisite into a hard failure.
"""
from __future__ import annotations

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
        yield async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
    finally:
        await engine.dispose()


async def _insert_run(factory, run_id: str, *, protocol_version: int = 2) -> None:
    async with factory() as db:
        await db.execute(
            text(
                "INSERT INTO lab_runs "
                "(id, task_id, researcher_slug, adapter, status, protocol_version) "
                "VALUES (:id, :task, 'sage', 'simverse_ref', 'queued', :version)"
            ),
            {"id": run_id, "task": f"task-{run_id}", "version": protocol_version},
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
        self.factory = factory
        self.host_lost = host_lost
        self.create_calls: list[tuple[str, int]] = []
        self.reattach_calls: list[tuple[str, int]] = []
        self.locators: dict[tuple[str, int], str] = {}
        self.registration_seen_before_create = False

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
    run_id = f"reattach-{uuid.uuid4()}"
    await _insert_run(pg_factory, run_id)
    provider = RecordingProvider(pg_factory)

    async with pg_factory() as db:
        with pytest.raises(CrashAfterProviderCreate):
            await runtime_sessions.create_or_reattach(
                db, run_id=run_id, epoch=9, provider=provider,
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
            db, run_id=run_id, epoch=9, provider=provider,
            durability_class="session_affine",
        )
    assert ready.status == "ready"
    assert ready.client_run_id == creating["client_run_id"]
    assert len(provider.create_calls) == 1
    assert provider.reattach_calls == [(creating["client_run_id"], 9)]

    # A ready retry is local and cannot manufacture a second provider session.
    async with pg_factory() as db:
        same = await runtime_sessions.create_or_reattach(
            db, run_id=run_id, epoch=9, provider=provider,
            durability_class="session_affine",
        )
    assert same.id == ready.id
    assert len(provider.create_calls) == 1
    assert len(provider.reattach_calls) == 1


@pytest.mark.anyio
async def test_session_affine_host_loss_quarantines_instead_of_recreating(pg_factory):
    runtime_sessions = _runtime_sessions_api()
    run_id = f"host-loss-{uuid.uuid4()}"
    await _insert_run(pg_factory, run_id)
    provider = RecordingProvider(pg_factory, host_lost=True)

    async with pg_factory() as db:
        with pytest.raises(CrashAfterProviderCreate):
            await runtime_sessions.create_or_reattach(
                db, run_id=run_id, epoch=4, provider=provider,
                durability_class="session_affine",
            )
    async with pg_factory() as db:
        try:
            await runtime_sessions.create_or_reattach(
                db, run_id=run_id, epoch=4, provider=provider,
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
