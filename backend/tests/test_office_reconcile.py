"""Safe reconciliation of the two intentional office/duty overlaps."""
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.office import Office
from app.models.resident import Resident
from app.services import duty_service
from app.services.office_service import OfficeService, reconcile_seed_offices


def _resident(slug: str, duty: str) -> Resident:
    return Resident(
        slug=slug,
        name=slug,
        district="town_hall",
        status="idle",
        resident_type="npc",
        tile_x=1,
        tile_y=1,
        meta_json={"duty": {"key": duty}},
    )


def _office(key: str, holder: str | None = None) -> Office:
    return Office(
        office_key=key,
        holder_slug=holder,
        institution="town_hall" if key == "town_clerk" else "post_office",
        fill_strategy="seed",
        perms_json={},
    )


@pytest.fixture
def quiet_office_events(monkeypatch):
    async def _quiet(*args, **kwargs):
        return False

    monkeypatch.setattr(OfficeService, "_emit_office_changed", _quiet)


@pytest.mark.anyio
async def test_dry_run_then_apply_is_idempotent(db_session, quiet_office_events):
    db_session.add_all([
        _resident("zhao-qiwen", "town_clerk"),
        _resident("luo-xiaozhou", "postman"),
        _office("town_clerk"),
        _office("postman"),
    ])
    await db_session.commit()

    dry = await reconcile_seed_offices(db_session)
    assert dry["dry_run"] is True
    assert {x["office_key"] for x in dry["would_appoint"]} == {
        "town_clerk", "postman",
    }
    assert set((await db_session.execute(
        select(Office.holder_slug)
    )).scalars().all()) == {None}

    applied = await reconcile_seed_offices(db_session, apply=True)
    assert {x["office_key"] for x in applied["appointed"]} == {
        "town_clerk", "postman",
    }
    again = await reconcile_seed_offices(db_session, apply=True)
    assert len(again["unchanged"]) == 2
    assert again["appointed"] == []


@pytest.mark.anyio
async def test_nonempty_conflict_is_reported_never_overwritten(
    db_session, quiet_office_events,
):
    db_session.add_all([
        _resident("zhao-qiwen", "town_clerk"),
        _resident("luo-xiaozhou", "postman"),
        _office("town_clerk", "someone-else"),
        _office("postman", "luo-xiaozhou"),
    ])
    await db_session.commit()

    report = await reconcile_seed_offices(db_session, apply=True)
    assert report["conflicts"]["town_clerk"] == {
        "office_holder": "someone-else",
        "duty_holder": "zhao-qiwen",
    }
    assert await OfficeService(db_session).get_holder("town_clerk") == "someone-else"

    assert not await OfficeService(db_session).appoint_if_vacant(
        "town_clerk", "zhao-qiwen", fill_strategy="seed",
    )
    assert await OfficeService(db_session).get_holder("town_clerk") == "someone-else"


@pytest.mark.anyio
async def test_office_lookup_validates_duty_then_falls_back(
    db_session, monkeypatch, caplog,
):
    monkeypatch.setattr(settings, "polis_office_enabled", True)
    actual = _resident("zhao-qiwen", "town_clerk")
    wrong = _resident("wrong-holder", "postman")
    db_session.add_all([actual, wrong, _office("town_clerk", "wrong-holder")])
    await db_session.commit()

    with caplog.at_level("ERROR"):
        found = await duty_service.find_duty_resident(db_session, "town_clerk")
    assert found is not None and found.slug == "zhao-qiwen"
    assert any("office/duty mismatch" in row.message for row in caplog.records)


def test_roster_reset_calls_reconciliation_after_seeding():
    source = (Path(__file__).resolve().parent.parent
              / "seed" / "reset_builtin_residents.py").read_text()
    assert source.index("created = await seed_presets(db)") < source.index(
        "reconcile_seed_offices(db, apply=True)"
    )


def test_ops_script_defaults_to_dry_run():
    source = (Path(__file__).resolve().parent.parent
              / "scripts" / "reconcile_seed_offices.py").read_text()
    assert '"--apply", action="store_true"' in source
    assert "reconcile_seed_offices(db, apply=apply)" in source
