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
