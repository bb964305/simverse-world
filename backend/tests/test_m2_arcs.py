"""M2 story-arc engine tests: seeding, sequential triggers, finale, digest."""
import pytest
from sqlalchemy import select

from app.models.resident import Resident
from app.models.resident_goal import ResidentGoal
from app.services import arc_service, relation_service


def _res(slug, name, tile=(70, 56)):
    return Resident(slug=slug, name=name, district="central_plaza", status="idle",
                    resident_type="npc", creator_id="sys", tile_x=tile[0], tile_y=tile[1])


async def _arc(db, resident_id, title, milestones):
    g = ResidentGoal(resident_id=resident_id, kind="arc", title=title, status="active")
    g.milestones_json = [{"title": m["title"], "done": False, "trigger": m["trigger"]}
                         for m in milestones]
    db.add(g)
    await db.commit()
    await db.refresh(g)
    return g


@pytest.mark.anyio
async def test_relation_trigger_advances_in_order(db_session):
    a = _res("a", "甲")
    b = _res("b", "乙")
    db_session.add_all([a, b])
    await db_session.commit()
    goal = await _arc(db_session, a.id, "test", [
        {"title": "m1", "trigger": {"type": "relation", "with": "b", "affinity_gte": 0.5}},
        {"title": "m2", "trigger": {"type": "relation", "with": "b", "affinity_gte": 0.8}},
    ])

    # No relation yet → nothing advances.
    assert await arc_service.evaluate_arcs(db_session) == 0

    # affinity 0.6 → only m1 completes (sequential: m2 not checked).
    await relation_service.bump(db_session, a.id, b.id, d_affinity=0.6)
    assert await arc_service.evaluate_arcs(db_session) == 1
    await db_session.refresh(goal)
    assert goal.milestones_json[0]["done"] is True
    assert goal.milestones_json[1]["done"] is False
    assert goal.status == "active"

    # push affinity to 0.85 → m2 completes → finale.
    await relation_service.bump(db_session, a.id, b.id, d_affinity=0.25)
    assert await arc_service.evaluate_arcs(db_session) == 1
    await db_session.refresh(goal)
    assert goal.status == "achieved"
    assert goal.progress == 1.0


@pytest.mark.anyio
async def test_co_location_counter(db_session):
    a = _res("a", "甲", tile=(77, 19))   # inside tavern bounds
    b = _res("b", "乙", tile=(77, 19))
    db_session.add_all([a, b])
    await db_session.commit()
    goal = await _arc(db_session, a.id, "co", [
        {"title": "twice", "trigger": {"type": "co_location", "with": "b",
                                       "location": "tavern", "times": 2}},
    ])
    # First night: counter 1, not done.
    assert await arc_service.evaluate_arcs(db_session) == 0
    await db_session.refresh(goal)
    assert goal.milestones_json[0]["_count"] == 1
    # Second night: counter 2 → done → finale.
    assert await arc_service.evaluate_arcs(db_session) == 1
    await db_session.refresh(goal)
    assert goal.status == "achieved"


@pytest.mark.anyio
async def test_co_location_requires_both_present(db_session):
    a = _res("a", "甲", tile=(77, 19))     # in tavern
    b = _res("b", "乙", tile=(0, 0))        # elsewhere
    db_session.add_all([a, b])
    await db_session.commit()
    goal = await _arc(db_session, a.id, "co", [
        {"title": "once", "trigger": {"type": "co_location", "with": "b",
                                      "location": "tavern", "times": 1}},
    ])
    assert await arc_service.evaluate_arcs(db_session) == 0
    await db_session.refresh(goal)
    assert goal.milestones_json[0].get("_count", 0) == 0


@pytest.mark.anyio
async def test_count_metric_memory(db_session):
    from app.memory.service import MemoryService
    a = _res("a", "甲")
    db_session.add(a)
    await db_session.commit()
    goal = await _arc(db_session, a.id, "cnt", [
        {"title": "three", "trigger": {"type": "count", "metric": "memory", "gte": 3}},
    ])
    svc = MemoryService(db_session)
    for i in range(2):
        await svc.add_memory(a.id, "event", f"m{i}", 0.5, "observation")
    assert await arc_service.evaluate_arcs(db_session) == 0  # only 2
    await svc.add_memory(a.id, "event", "m3", 0.5, "observation")
    assert await arc_service.evaluate_arcs(db_session) == 1  # 3 (arc mem also counts)


@pytest.mark.anyio
async def test_finale_bulletin_and_relation_bump(db_session):
    from app.models.bulletin_post import BulletinPost
    a = _res("a", "阿岚")
    b = _res("b", "陈铁生")
    db_session.add_all([a, b])
    await db_session.commit()
    await relation_service.bump(db_session, a.id, b.id, d_affinity=0.6)
    goal = await _arc(db_session, a.id, "和解", [
        {"title": "回暖", "trigger": {"type": "relation", "with": "b", "affinity_gte": 0.5}},
    ])
    await arc_service.evaluate_arcs(db_session)

    await db_session.refresh(goal)
    assert goal.status == "achieved"
    post = (await db_session.execute(
        select(BulletinPost).where(BulletinPost.author_resident_id == a.id)
    )).scalars().first()
    assert post is not None
    pair = await relation_service.get_pair(db_session, a.id, b.id)
    assert pair.affinity > 0.6  # finale bump applied


@pytest.mark.anyio
async def test_flag_off_no_advance(db_session, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "arc_engine_enabled", False)
    a = _res("a", "甲")
    b = _res("b", "乙")
    db_session.add_all([a, b])
    await db_session.commit()
    await relation_service.bump(db_session, a.id, b.id, d_affinity=0.9)
    await _arc(db_session, a.id, "x", [
        {"title": "m", "trigger": {"type": "relation", "with": "b", "affinity_gte": 0.5}},
    ])
    assert await arc_service.evaluate_arcs(db_session) == 0


@pytest.mark.anyio
async def test_seed_arcs_present(db_session):
    from seed.preset_characters import PRESET_ARCS, seed_preset_arcs, PRESET_CHARACTERS
    from app.models.user import User
    from seed.preset_characters import seed_presets

    db_session.add(User(id="00000000-0000-0000-0000-000000000001", name="Sys",
                        email="s@x.io", soul_coin_balance=0))
    await db_session.commit()
    await seed_presets(db_session)

    arcs = (await db_session.execute(
        select(ResidentGoal).where(ResidentGoal.kind == "arc", ResidentGoal.status == "active")
    )).scalars().all()
    assert len(arcs) == len(PRESET_ARCS) == 5
    for g in arcs:
        assert g.milestones_json and all("trigger" in m for m in g.milestones_json)

    # idempotent
    assert await seed_preset_arcs(db_session) == 0
