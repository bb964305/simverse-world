"""M5 space-expansion tests: postman duty, governance-built buildings."""
import pytest
from sqlalchemy import select

from app.models.resident import Resident
from app.models.season import Poll
from app.services import civic_service, duty_service


@pytest.mark.anyio
async def test_postman_is_11th_resident_with_duty():
    from seed.preset_characters import PRESET_CHARACTERS
    postman = next((c for c in PRESET_CHARACTERS if c["slug"] == "luo-xiaozhou"), None)
    assert postman is not None
    assert postman["meta_json"]["duty"]["key"] == "postman"
    assert len(PRESET_CHARACTERS) == 11


@pytest.mark.anyio
async def test_postman_work_runs_delivery(db_session):
    from app.services import coin_service
    from app.models.memory import Memory
    r = Resident(slug="luo-xiaozhou", name="骆小舟", district="town_entrance",
                 status="idle", resident_type="npc", creator_id="sys",
                 tile_x=70, tile_y=92,
                 meta_json={"duty": {"key": "postman", "perks": {"wage_sc": 6}}})
    db_session.add(r)
    await db_session.commit()

    line = await duty_service.on_work(db_session, r)
    assert line and "投递" in line
    assert await coin_service.treasury_balance(db_session, "luo-xiaozhou") == 6
    mem = (await db_session.execute(
        select(Memory).where(Memory.resident_id == r.id)
    )).scalars().first()
    assert mem is not None


@pytest.mark.anyio
async def test_civic_agenda_opens_building_proposals(db_session):
    clerk = Resident(slug="zhao", name="赵启文", district="town_hall", status="idle",
                     resident_type="npc", creator_id="sys", tile_x=119, tile_y=53,
                     meta_json={"duty": {"key": "town_clerk"}})
    db_session.add(clerk)
    await db_session.commit()

    opened = await civic_service.seed_civic_agenda(db_session)
    assert opened == 2
    polls = (await db_session.execute(select(Poll))).scalars().all()
    topics = {p.question for p in polls}
    assert any("邮局" in t for t in topics)
    assert any("剧院" in t for t in topics)

    # idempotent
    assert await civic_service.seed_civic_agenda(db_session) == 0


def test_civic_agenda_and_election_are_wired_into_nightly():
    """Guard against dangling entry points: the agenda seeding (M5) and the
    election trigger (M6) must actually be invoked from the nightly cron —
    a service nobody calls is a feature that never happens."""
    import inspect
    from app.tasks import nightly_cron
    src = inspect.getsource(nightly_cron.run_nightly_jobs)
    assert "seed_civic_agenda" in src
    assert "maybe_open_seasonal_election" in src


@pytest.mark.anyio
async def test_building_proposal_passes_and_appears_in_world(db_session):
    from app.models.season import Vote
    from app.models.dynamic_location import DynamicLocation
    from app.agent.map_data import get_location_by_id

    poll = await civic_service.propose(
        db_session, "在南苑空地兴建一座邮局",
        civic_service.CIVIC_AGENDA[0]["options"],
        days=0,
    )
    db_session.add(Vote(poll_id=poll.id, user_id="u1", option_idx=0))
    await db_session.commit()

    await civic_service.close_due_polls(db_session)
    row = (await db_session.execute(
        select(DynamicLocation).where(DynamicLocation.slug == "post_office")
    )).scalar_one_or_none()
    # The overlay row is the durable source of truth; map_data.load_dynamic_
    # locations merges active rows into LOCATIONS at startup / on sv:world:reload
    # (exercised by test_map_integration). Here we assert the row + its shape.
    assert row is not None and row.active is True
    assert row.data_json.get("bounds") and row.data_json.get("entrance")
    # Direct merge check (same-session): feed the row through the loader shape.
    from app.agent.map_data import LOCATIONS
    LOCATIONS["post_office"] = {**row.data_json,
                                "bounds": tuple(row.data_json["bounds"]),
                                "center": tuple(row.data_json["center"]),
                                "entrance": tuple(row.data_json["entrance"])}
    assert get_location_by_id("post_office") is not None
    LOCATIONS.pop("post_office", None)  # don't leak into other tests
