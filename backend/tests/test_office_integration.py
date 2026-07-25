"""S2-1 offices — integration tests.

Covers the migration seed/backfill (task 1) and, from task 2 on, the mayor
write/read rerouting, the wage-bonus regression door (gotcha #1), the duty
lookup rerouting and the byte-level gate-off fallback.

Migration note: the full alembic chain is not sqlite-runnable from scratch
(pre-existing: 003 does non-batch ALTERs; dev uses create_all, prod is
Postgres — the chain is exercised in tests/integration on real PG). The
backfill is therefore tested by driving the migration module's own
``seed_offices`` / ``backfill_holders`` helpers against a real bind, which is
exactly what ``upgrade()`` executes after ``create_table``.
"""
import importlib.util
import json
import pathlib

import pytest
from sqlalchemy import select, text

from app.config import settings
from app.models.office import Office
from app.models.resident import Resident

_MIG_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "alembic" / "versions" / "NNN_add_offices.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_add_offices", _MIG_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _res(slug, name, meta=None, **kw):
    d = dict(slug=slug, name=name, district="central_plaza", status="idle",
             resident_type="npc", creator_id="sys", tile_x=70, tile_y=56,
             meta_json=meta)
    d.update(kw)
    return Resident(**d)


@pytest.mark.anyio
async def test_migration_backfills_existing_mayor_clerk_postman(db_session):
    """Seed + backfill land the four office rows from today's stores, and a
    second run changes nothing (idempotent)."""
    from app.services.config_service import ConfigService

    db_session.add_all([
        _res("zhao-qiwen", "赵启文", meta={"duty": {"key": "town_clerk"}}),
        _res("luo-xiaozhou", "骆小舟", meta={"duty": {"key": "postman",
                                                    "perks": {"wage_sc": 6}}}),
        _res("he-qiaoyun", "何巧云", meta={"mayor": True}),
    ])
    await db_session.commit()
    await ConfigService(db_session).set(
        "current_mayor", "he-qiaoyun", group="civic", updated_by="test")

    mig = _load_migration()
    conn = await db_session.connection()

    inserted = await conn.run_sync(lambda sc: mig.seed_offices(sc))
    filled = await conn.run_sync(lambda sc: mig.backfill_holders(sc))
    await db_session.commit()
    assert inserted == 4
    assert filled == 3  # mayor + clerk + postman; doctor stays NULL

    rows = {o.office_key: o for o in
            (await db_session.execute(select(Office))).scalars().all()}
    assert set(rows) == {"mayor", "town_clerk", "postman", "doctor"}
    assert rows["mayor"].holder_slug == "he-qiaoyun"
    assert rows["town_clerk"].holder_slug == "zhao-qiwen"
    assert rows["postman"].holder_slug == "luo-xiaozhou"
    assert rows["doctor"].holder_slug is None          # greenfield (S5-8)
    assert rows["doctor"].institution == "clinic"
    assert rows["doctor"].fill_strategy == "appointment"

    # meta_json['mayor'] untouched — it is the wage multiplier, not identity
    mayor = (await db_session.execute(
        select(Resident).where(Resident.slug == "he-qiaoyun"))).scalar_one()
    assert (mayor.meta_json or {}).get("mayor") is True

    # idempotent second run: nothing inserted, nothing re-filled
    conn = await db_session.connection()
    assert await conn.run_sync(lambda sc: mig.seed_offices(sc)) == 0
    assert await conn.run_sync(lambda sc: mig.backfill_holders(sc)) == 0
    await db_session.commit()


# ── task 2: mayor write/read rerouting ────────────────────────────────

@pytest.mark.anyio
async def test_install_mayor_dual_writes_office_when_gate_on(db_session, monkeypatch):
    """Gate on → install_mayor keeps BOTH legacy stores alive (meta_json
    ['mayor'] + system_config) AND lands the offices row (dual-write)."""
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    from app.services import election_service
    from app.services.config_service import ConfigService
    from app.services.office_service import OfficeService

    winner = _res("cand", "候选人")
    db_session.add_all([winner, _res("other", "路人", meta={"mayor": True})])
    await db_session.commit()

    assert await election_service.install_mayor(db_session, "cand") is True

    await db_session.refresh(winner)
    assert (winner.meta_json or {}).get("mayor") is True          # wage store
    assert await ConfigService(db_session).get("current_mayor") == "cand"
    assert await OfficeService(db_session).get_holder("mayor") == "cand"
    other = (await db_session.execute(
        select(Resident).where(Resident.slug == "other"))).scalar_one()
    assert not (other.meta_json or {}).get("mayor")


@pytest.mark.anyio
async def test_current_mayor_reads_office_then_falls_back_to_config(db_session, monkeypatch):
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    from app.services import election_service
    from app.services.config_service import ConfigService
    from app.services.office_service import OfficeService

    # offices empty (e.g. gate flipped on before any election) → config fallback
    await ConfigService(db_session).set(
        "current_mayor", "legacy-mayor", group="civic", updated_by="test")
    assert await election_service.current_mayor(db_session) == "legacy-mayor"

    # offices holds a mayor → offices wins
    await OfficeService(db_session).appoint("mayor", "office-mayor",
                                            fill_strategy="election")
    assert await election_service.current_mayor(db_session) == "office-mayor"


@pytest.mark.anyio
async def test_pay_wage_bonus_preserved_when_gate_on(db_session, monkeypatch):
    """Gotcha #1 regression door: with the gate ON the sitting mayor still
    earns wage × election_mayor_wage_bonus — the dual-write must keep the
    meta_json['mayor'] multiplier alive."""
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    from app.services import election_service, duty_service, coin_service

    mayor = _res("cand", "候选人",
                 meta={"duty": {"key": "shop_keeper", "perks": {"wage_sc": 10}}})
    db_session.add_all([mayor, _res("other", "路人")])
    await db_session.commit()
    assert await election_service.install_mayor(db_session, "cand") is True
    await db_session.refresh(mayor)

    from app.models.shop import Item
    db_session.add(Item(code="x", kind="consumable", name="X", price_sc=5))
    await db_session.commit()
    await duty_service.on_work(db_session, mayor)
    expected = round(10 * settings.election_mayor_wage_bonus)
    assert await coin_service.treasury_balance(db_session, "cand") == expected


@pytest.mark.anyio
async def test_execute_outcome_mayor_branch_still_installs(db_session, monkeypatch):
    """Election outcome → _execute_outcome mayor branch → install_mayor →
    offices row lands (the civic_service.py dispatcher path stays intact)."""
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    from app.services import civic_service
    from app.services.office_service import OfficeService

    db_session.add_all([_res("cand", "候选人"), _res("other", "路人")])
    await db_session.commit()

    applied = await civic_service._execute_outcome(
        db_session, {"type": "mayor", "slug": "cand"})
    assert applied is True
    assert await OfficeService(db_session).get_holder("mayor") == "cand"


@pytest.mark.anyio
async def test_gate_off_byte_level_fallback(db_session, monkeypatch):
    """Gate off → install_mayor / current_mayor / _pay_wage behave exactly as
    today: legacy stores written, offices table untouched."""
    monkeypatch.setattr(settings, "polis_office_enabled", False)
    from app.services import election_service, duty_service, coin_service
    from app.services.config_service import ConfigService

    mayor = _res("cand", "候选人",
                 meta={"duty": {"key": "shop_keeper", "perks": {"wage_sc": 10}}})
    db_session.add_all([mayor, _res("other", "路人")])
    await db_session.commit()

    assert await election_service.install_mayor(db_session, "cand") is True
    await db_session.refresh(mayor)
    assert (mayor.meta_json or {}).get("mayor") is True
    assert await ConfigService(db_session).get("current_mayor") == "cand"
    assert await election_service.current_mayor(db_session) == "cand"

    from app.models.shop import Item
    db_session.add(Item(code="x", kind="consumable", name="X", price_sc=5))
    await db_session.commit()
    await duty_service.on_work(db_session, mayor)
    expected = round(10 * settings.election_mayor_wage_bonus)
    assert await coin_service.treasury_balance(db_session, "cand") == expected

    rows = (await db_session.execute(select(Office))).scalars().all()
    assert rows == []  # offices never read nor written with the gate off


@pytest.mark.anyio
async def test_migration_backfill_tolerates_empty_world(db_session):
    """No residents, no system_config → four vacant rows, no crash."""
    mig = _load_migration()
    conn = await db_session.connection()
    assert await conn.run_sync(lambda sc: mig.seed_offices(sc)) == 4
    assert await conn.run_sync(lambda sc: mig.backfill_holders(sc)) == 0
    await db_session.commit()
    rows = (await db_session.execute(select(Office))).scalars().all()
    assert len(rows) == 4 and all(o.holder_slug is None for o in rows)
