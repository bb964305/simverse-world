"""M3 civic governance tests: propose, NPC voting, close + execute outcomes,
lecture→debate."""
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.resident import Resident
from app.models.season import Poll, Vote
from app.services import civic_service


def _res(slug, name, duty=None, sbti=None, **kw):
    meta = {}
    if duty:
        meta["duty"] = duty
    if sbti:
        meta["sbti"] = {"dimensions": sbti}
    d = dict(slug=slug, name=name, district="town_hall", status="idle",
             resident_type="npc", creator_id="sys", tile_x=119, tile_y=53,
             meta_json=meta or None)
    d.update(kw)
    return Resident(**d)


@pytest.mark.anyio
async def test_propose_creates_poll_and_clerk_notice(db_session):
    from app.models.bulletin_post import BulletinPost
    clerk = _res("zhao", "赵启文", duty={"key": "town_clerk"})
    db_session.add(clerk)
    await db_session.commit()

    poll = await civic_service.propose(
        db_session, "广场是否加装长椅",
        [{"label": "支持", "effect": {"type": "narrative", "event": {"title": "长椅到位"}}},
         {"label": "维持现状", "effect": None}],
    )
    assert poll is not None and poll.status == "open"
    note = (await db_session.execute(
        select(BulletinPost).where(BulletinPost.kind == "notice")
    )).scalars().first()
    assert note is not None and note.author_resident_id == clerk.id


@pytest.mark.anyio
async def test_npc_voting_is_rule_based_and_idempotent(db_session):
    # conservative resident (A2=H) prefers the no-effect status-quo option.
    cons = _res("cons", "守序", sbti={"A2": "H"})
    db_session.add(cons)
    await db_session.commit()
    poll = await civic_service.propose(
        db_session, "要不要大改",
        [{"label": "大改", "effect": {"type": "narrative", "event": {"title": "x"}}},
         {"label": "维持现状", "effect": None}],
    )
    n = await civic_service.run_npc_voting(db_session)
    assert n == 1
    await db_session.refresh(poll)
    # status-quo (index 1, no effect) should have the conservative's vote
    assert poll.options_json[1]["npc_votes"] == 1
    # idempotent: same resident doesn't double-vote
    assert await civic_service.run_npc_voting(db_session) == 0


@pytest.mark.anyio
async def test_npc_vote_leans_toward_liked_proposer(db_session):
    """A warm tie to the proposer flips even a conservative toward the lead
    option; without the tie the conservative stays with the status quo."""
    from app.services import relation_service

    proposer = _res("prop", "提案人")
    fan = _res("fan", "老友", sbti={"A2": "H"})       # conservative but close
    cons = _res("cons2", "守序路人", sbti={"A2": "H"})  # conservative, no tie
    db_session.add_all([proposer, fan, cons])
    await db_session.commit()
    await relation_service.bump(db_session, fan.id, proposer.id, d_affinity=0.9)

    poll = await civic_service.propose(
        db_session, "把广场喷泉修起来",
        [{"label": "赞成", "effect": {"type": "narrative", "event": {"title": "喷泉动工"}}},
         {"label": "维持现状", "effect": None}],
        proposer_slug="prop",
    )
    assert poll.options_json[0]["_proposer_slug"] == "prop"

    n = await civic_service.run_npc_voting(db_session)
    assert n == 3
    await db_session.refresh(poll)
    # proposer backs own proposal + the friend follows → 2 votes on option 0;
    # the unattached conservative keeps the status quo.
    assert poll.options_json[0]["npc_votes"] == 2
    assert poll.options_json[1]["npc_votes"] == 1


@pytest.mark.anyio
async def test_propose_notice_names_proposer(db_session):
    from app.models.bulletin_post import BulletinPost
    db_session.add_all([
        _res("zhao", "赵启文", duty={"key": "town_clerk"}),
        _res("jiang", "江临"),
    ])
    await db_session.commit()

    await civic_service.propose(
        db_session, "旱季供水改造",
        [{"label": "赞成", "effect": None}, {"label": "反对", "effect": None}],
        proposer_slug="jiang",
    )
    note = (await db_session.execute(
        select(BulletinPost).where(BulletinPost.kind == "notice")
    )).scalars().first()
    assert note is not None and "江临" in note.content_md and "提议" in note.content_md


@pytest.mark.anyio
async def test_close_executes_system_config_outcome(db_session):
    from app.services.config_service import ConfigService
    poll = await civic_service.propose(
        db_session, "延长酒馆营业",
        [{"label": "延长", "effect": {"type": "system_config",
          "key": "tavern_close_hour", "value": 23, "group": "civic"}},
         {"label": "不变", "effect": None}],
        days=0,  # closes immediately
    )
    # one player vote for option 0
    db_session.add(Vote(poll_id=poll.id, user_id="u1", option_idx=0))
    await db_session.commit()

    closed = await civic_service.close_due_polls(db_session)
    assert closed == 1
    await db_session.refresh(poll)
    assert poll.status == "closed"
    assert await ConfigService(db_session).get("tavern_close_hour") == 23


@pytest.mark.anyio
async def test_close_executes_dynamic_location_outcome(db_session, monkeypatch):
    from app.models.dynamic_location import DynamicLocation
    # avoid real redis publish in reload path
    poll = await civic_service.propose(
        db_session, "在南苑建邮局",
        [{"label": "建", "effect": {"type": "dynamic_location", "data": {
            "slug": "post_office", "name": "邮局", "type": "public",
            "bounds": [44, 100, 48, 106], "center": [46, 103], "entrance": [46, 100],
            "description": "小镇邮局", "boosted_actions": ["WORK"]}}},
         {"label": "不建", "effect": None}],
        days=0,
    )
    db_session.add(Vote(poll_id=poll.id, user_id="u1", option_idx=0))
    await db_session.commit()

    await civic_service.close_due_polls(db_session)
    row = (await db_session.execute(
        select(DynamicLocation).where(DynamicLocation.slug == "post_office")
    )).scalar_one_or_none()
    assert row is not None and row.active is True


@pytest.mark.anyio
async def test_no_effect_option_wins_no_execution(db_session):
    poll = await civic_service.propose(
        db_session, "advisory",
        [{"label": "A", "effect": None}, {"label": "B", "effect": None}],
        days=0,
    )
    closed = await civic_service.close_due_polls(db_session)
    assert closed == 1  # closes cleanly even with no effect


@pytest.mark.anyio
async def test_disabled_flag_blocks_propose(db_session, monkeypatch):
    monkeypatch.setattr(settings, "civic_polls_enabled", False)
    poll = await civic_service.propose(db_session, "x", [{"label": "a"}, {"label": "b"}])
    assert poll is None


@pytest.mark.anyio
async def test_lecture_end_spawns_debate(db_session):
    from app.models.debate import Debate
    db_session.add_all([
        _res("gu", "顾明远", duty={"key": "lecturer"}, sbti={"So1": "M", "A1": "H"}),
        _res("opt", "乐观者", sbti={"So1": "H", "A1": "H"}, district="cafe", tile_x=57, tile_y=20),
        _res("skept", "怀疑者", sbti={"So1": "H", "A1": "L"}, district="tavern", tile_x=77, tile_y=19),
    ])
    await db_session.commit()

    event = {"title": "小镇的来路的公开课", "payload_json": {"duty": "lecturer"}}
    ok = await civic_service.maybe_spawn_lecture_debate(db_session, event)
    assert ok is True
    d = (await db_session.execute(select(Debate))).scalars().first()
    assert d is not None
