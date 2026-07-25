"""S2-1 offices — OfficeService unit tests (task 1).

Atomicity: appoint/vacate are conditional-UPDATE + upsert (coin_service
pattern, no SELECT-then-write). term_check uses an injected ``now`` (frozen
clock) — no wall-clock flakiness. Gate-off behavior is a hard door: the
business paths must not touch the offices table when polis_office_enabled is
False.
"""
import asyncio
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.office import Office
from app.models.resident import Resident
from app.services.office_service import OfficeService


def _res(slug, name, meta=None):
    return Resident(
        slug=slug, name=name, district="central_plaza", status="idle",
        resident_type="npc", creator_id="sys", tile_x=70, tile_y=56,
        meta_json=meta,
    )


@pytest.mark.anyio
async def test_appoint_transfers_holder_atomically(db_session):
    svc = OfficeService(db_session)
    assert await svc.appoint("doctor", "doc-a", fill_strategy="appointment") is True
    assert await svc.get_holder("doctor") == "doc-a"

    # transfer: same office, new holder — still exactly one row
    assert await svc.appoint("doctor", "doc-b", fill_strategy="appointment") is True
    assert await svc.get_holder("doctor") == "doc-b"
    rows = (await db_session.execute(
        select(Office).where(Office.office_key == "doctor")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].holder_slug == "doc-b"
    assert rows[0].institution == "clinic"


@pytest.mark.anyio
async def test_appoint_upsert_atomic_no_lost_update(tmp_path):
    """Concurrent appoints to the same office key: no lost update, no
    duplicate row — exactly one row survives holding one of the contenders
    (coin_service upsert-race pattern).

    Uses a FILE-backed sqlite DB: the in-memory StaticPool hands every
    session the same DBAPI connection, which fakes transaction isolation and
    can't exercise a real insert race. Separate file connections serialize
    through sqlite's lock — the genuine contention path."""
    from sqlalchemy.pool import NullPool
    from sqlalchemy.ext.asyncio import create_async_engine
    from app.database import Base

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/offices_race.db", poolclass=NullPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _appoint(slug):
        async with factory() as db:
            try:
                return await OfficeService(db).appoint(
                    "mayor", slug, fill_strategy="election"
                )
            except Exception:
                return False

    try:
        results = await asyncio.gather(*(_appoint(f"cand-{i}") for i in range(4)))
        assert any(results)

        async with factory() as db:
            rows = (await db.execute(
                select(Office).where(Office.office_key == "mayor")
            )).scalars().all()
            assert len(rows) == 1
            assert rows[0].holder_slug in {f"cand-{i}" for i in range(4)}
    finally:
        await engine.dispose()


@pytest.mark.anyio
async def test_appoint_upsert_race_integrityerror_falls_through(db_session, db_engine, monkeypatch):
    """Losing the insert race (UNIQUE office_key) must fall through to the
    conditional UPDATE, not blow up — deterministic simulation: a 'concurrent
    winner' inserts the row between our rowcount==0 check and our commit."""
    from sqlalchemy.exc import IntegrityError

    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    real_commit = db_session.commit
    state = {"raced": False}

    async def racing_commit():
        if not state["raced"]:
            state["raced"] = True
            # concurrent winner lands the row first, then our INSERT collides
            async with factory() as other:
                other.add(Office(
                    office_key="doctor", holder_slug="winner",
                    institution="clinic", fill_strategy="appointment",
                ))
                await other.commit()
            raise IntegrityError("INSERT INTO offices", {}, Exception("UNIQUE"))
        await real_commit()

    monkeypatch.setattr(db_session, "commit", racing_commit)
    svc = OfficeService(db_session)
    assert await svc.appoint("doctor", "doc-late", fill_strategy="appointment") is True
    monkeypatch.setattr(db_session, "commit", real_commit)

    rows = (await db_session.execute(
        select(Office).where(Office.office_key == "doctor")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].holder_slug == "doc-late"  # update won after the fall-through


@pytest.mark.anyio
async def test_vacate_clears_holder_and_term(db_session):
    svc = OfficeService(db_session)
    await svc.appoint("postman", "luo-xiaozhou", fill_strategy="seed", term_days=7)
    assert await svc.vacate("postman") is True
    row = (await db_session.execute(
        select(Office).where(Office.office_key == "postman")
    )).scalar_one()
    assert row.holder_slug is None
    assert row.term_ends_at is None
    # vacating an already-vacant office is a no-op, not an error
    assert await svc.vacate("postman") is False


@pytest.mark.anyio
async def test_get_holder_returns_none_when_vacant(db_session):
    svc = OfficeService(db_session)
    assert await svc.get_holder("doctor") is None  # no row at all
    await svc.appoint("doctor", "doc-a", fill_strategy="appointment")
    await svc.vacate("doctor")
    assert await svc.get_holder("doctor") is None  # row exists, vacant


@pytest.mark.anyio
async def test_term_check_expires_due_terms_only(db_session):
    svc = OfficeService(db_session)
    await svc.appoint("mayor", "old-mayor", fill_strategy="election", term_days=7)
    await svc.appoint("postman", "luo-xiaozhou", fill_strategy="seed", term_days=7)
    await svc.appoint("doctor", "doc-a", fill_strategy="appointment")  # unlimited

    # frozen clock: far in the future → both 7-world-day terms are due
    future = datetime.now(UTC) + timedelta(days=365)
    n = await svc.term_check(now=future)
    assert n == 2
    assert await svc.get_holder("mayor") is None
    assert await svc.get_holder("postman") is None
    assert await svc.get_holder("doctor") == "doc-a"  # NULL term never expires

    # frozen clock before any expiry → nothing happens
    await svc.appoint("mayor", "new-mayor", fill_strategy="election", term_days=7)
    past = datetime.now(UTC) - timedelta(days=365)
    assert await svc.term_check(now=past) == 0
    assert await svc.get_holder("mayor") == "new-mayor"


@pytest.mark.anyio
async def test_term_check_infinite_term_never_expires(db_session):
    svc = OfficeService(db_session)
    # term_days=None and term_days=0 both mean unlimited (byte-compatible
    # with today's overwrite-style mayor)
    await svc.appoint("mayor", "forever", fill_strategy="election", term_days=0)
    await svc.appoint("doctor", "doc-a", fill_strategy="appointment", term_days=None)
    future = datetime.now(UTC) + timedelta(days=10000)
    assert await svc.term_check(now=future) == 0
    assert await svc.get_holder("mayor") == "forever"
    assert await svc.get_holder("doctor") == "doc-a"


@pytest.mark.anyio
async def test_term_check_clears_mayor_meta_and_config(db_session):
    """Expiring the mayor's term must not leave a stale wage-bonus flag
    (gotcha #1: meta_json['mayor'] is the wage multiplier) nor a stale
    system_config['current_mayor'] fallback."""
    from app.services.config_service import ConfigService

    r = _res("old-mayor", "老镇长", meta={"mayor": True})
    db_session.add(r)
    await db_session.commit()
    await ConfigService(db_session).set(
        "current_mayor", "old-mayor", group="civic", updated_by="test")

    svc = OfficeService(db_session)
    await svc.appoint("mayor", "old-mayor", fill_strategy="election", term_days=7)
    n = await svc.term_check(now=datetime.now(UTC) + timedelta(days=365))
    assert n == 1
    await db_session.refresh(r)
    assert not (r.meta_json or {}).get("mayor")
    assert await ConfigService(db_session).get("current_mayor") is None


@pytest.mark.anyio
async def test_term_days_converted_via_world_clock(db_session):
    """term_days are WORLD days: with world_clock_k=4 a 8-world-day term ends
    ~2 real days out — never 8 real days (the utcnow-comparison bug)."""
    svc = OfficeService(db_session)
    await svc.appoint("mayor", "cand", fill_strategy="election", term_days=8)
    row = (await db_session.execute(
        select(Office).where(Office.office_key == "mayor")
    )).scalar_one()
    assert row.term_ends_at is not None
    ends = row.term_ends_at
    if ends.tzinfo is None:
        ends = ends.replace(tzinfo=UTC)
    real_span = (ends - datetime.now(UTC)).total_seconds()
    k = settings.world_clock_k
    expected = 8 * 86400 / k
    assert abs(real_span - expected) < 3600  # within an hour of 8/k real days


@pytest.mark.anyio
async def test_office_key_unique_constraint(db_session):
    db_session.add(Office(office_key="mayor", institution="town_hall",
                          fill_strategy="election"))
    await db_session.commit()
    db_session.add(Office(office_key="mayor", institution="town_hall",
                          fill_strategy="election"))
    with pytest.raises(Exception):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.anyio
async def test_list_offices_shape(db_session):
    svc = OfficeService(db_session)
    await svc.appoint("town_clerk", "zhao-qiwen", fill_strategy="seed")
    offices = await svc.list_offices()
    assert isinstance(offices, list) and len(offices) == 1
    o = offices[0]
    assert o["office_key"] == "town_clerk"
    assert o["holder_slug"] == "zhao-qiwen"
    assert o["institution"] == "town_hall"
    assert o["fill_strategy"] == "seed"
    assert "perms_json" in o and "term_started_at" in o and "term_ends_at" in o


@pytest.mark.anyio
async def test_gate_off_appoint_is_noop_or_not_wired(db_session, monkeypatch):
    """Gate off → the business path (install_mayor) never touches offices.
    Direct OfficeService calls remain usable (admin/tests), but nothing in
    the election flow writes the table."""
    monkeypatch.setattr(settings, "polis_office_enabled", False)
    from app.services import election_service

    db_session.add_all([_res("cand", "候选人"), _res("other", "路人")])
    await db_session.commit()
    assert await election_service.install_mayor(db_session, "cand") is True

    rows = (await db_session.execute(select(Office))).scalars().all()
    assert rows == []  # offices untouched with the gate off
