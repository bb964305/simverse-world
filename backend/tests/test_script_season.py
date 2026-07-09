"""C3 剧本季: script-act firing, polls (vote/dedup/close), finale settlement,
and season world-view prompt injection."""

import pytest
from datetime import datetime, UTC, timedelta

from sqlalchemy import select

from app.models.user import User
from app.models.resident import Resident
from app.models.season import Season, SeasonScript, Poll, Vote, SeasonScore
from app.models.world_event import WorldEvent
from app.models.bulletin_post import BulletinPost
from app.models.memory import Memory
from app.services import script_service as ss


async def _user(db, email, bal=0):
    u = User(name="u", email=email, soul_coin_balance=bal)
    db.add(u)
    await db.commit()
    return u


async def _season(db, status="active", ends_in_h=24, world_view=None):
    now = datetime.now(UTC)
    s = Season(title="谜案季", theme="小镇疑云", status=status,
               starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=ends_in_h),
               payload_json={"world_view": world_view} if world_view else {})
    db.add(s)
    await db.commit()
    return s


# --------------------------------------------------------------------------- #
# Script acts                                                                  #
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_fire_due_script_creates_event_clue_and_secret(db_session):
    s = await _season(db_session)
    res = Resident(slug="kdt", name="侦探", creator_id="system", district="cafe", status="idle", tile_x=1, tile_y=1)
    db_session.add(res)
    await db_session.commit()
    act = SeasonScript(
        season_id=s.id, act=1, trigger_at=datetime.now(UTC) - timedelta(minutes=1), status="pending",
        event_payload_json={
            "title": "钟楼疑云", "description": "钟楼午夜传出怪声。", "clue": "有人看到阁楼的灯亮着。",
            "secrets": [{"resident_slug": "kdt", "memory_content": "我知道钟楼的钥匙在谁手里。", "importance": 0.9}],
        },
    )
    db_session.add(act)
    await db_session.commit()

    fired = await ss.fire_due_scripts(db_session)
    assert len(fired) == 1 and fired[0]["secrets_injected"] == 1

    we = (await db_session.execute(select(WorldEvent).where(WorldEvent.type == "script"))).scalar_one()
    assert we.is_active and we.title == "钟楼疑云"
    clue = (await db_session.execute(select(BulletinPost).where(BulletinPost.kind == "clue"))).scalar_one()
    assert "阁楼" in clue.content_md
    mem = (await db_session.execute(select(Memory).where(Memory.source == "script"))).scalar_one()
    assert mem.resident_id == res.id and mem.importance == 0.9

    await db_session.refresh(act)
    assert act.status == "fired"
    # Idempotent: a second pass fires nothing more.
    assert await ss.fire_due_scripts(db_session) == []


@pytest.mark.anyio
async def test_future_script_not_fired(db_session):
    s = await _season(db_session)
    act = SeasonScript(season_id=s.id, act=1, trigger_at=datetime.now(UTC) + timedelta(hours=2),
                       status="pending", event_payload_json={"title": "以后"})
    db_session.add(act)
    await db_session.commit()
    assert await ss.fire_due_scripts(db_session) == []


# --------------------------------------------------------------------------- #
# Polls                                                                        #
# --------------------------------------------------------------------------- #
async def _poll(db, season_id, closes_in_h=24, status="open"):
    p = Poll(season_id=season_id, question="谁是凶手？", options_json=["管家", "园丁", "医生"],
             closes_at=datetime.now(UTC) + timedelta(hours=closes_in_h), status=status)
    db.add(p)
    await db.commit()
    return p


@pytest.mark.anyio
async def test_open_polls_and_vote(db_session):
    s = await _season(db_session)
    p = await _poll(db_session, s.id)
    listed = await ss.open_polls(db_session, s.id)
    assert len(listed) == 1 and listed[0]["question"] == "谁是凶手？"

    u = await _user(db_session, "v1@c.com")
    await ss.cast_vote(db_session, p.id, u.id, 1)
    v = (await db_session.execute(select(Vote).where(Vote.poll_id == p.id))).scalar_one()
    assert v.option_idx == 1


@pytest.mark.anyio
async def test_vote_dedup_and_validation(db_session):
    s = await _season(db_session)
    p = await _poll(db_session, s.id)
    u = await _user(db_session, "v2@c.com")
    await ss.cast_vote(db_session, p.id, u.id, 0)
    with pytest.raises(ss.PollError):
        await ss.cast_vote(db_session, p.id, u.id, 2)  # one vote per user
    with pytest.raises(ss.PollError):
        await ss.cast_vote(db_session, p.id, u.id, 9)  # (also) out of range

    u2 = await _user(db_session, "v3@c.com")
    with pytest.raises(ss.PollError):
        await ss.cast_vote(db_session, p.id, u2.id, 99)  # out of range


@pytest.mark.anyio
async def test_closed_poll_rejects_vote(db_session):
    s = await _season(db_session)
    p = await _poll(db_session, s.id, closes_in_h=-1)  # already closed by time
    u = await _user(db_session, "v4@c.com")
    with pytest.raises(ss.PollError):
        await ss.cast_vote(db_session, p.id, u.id, 0)


# --------------------------------------------------------------------------- #
# Finale                                                                       #
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_settle_due_season_closes_polls_and_scores(db_session):
    s = await _season(db_session, ends_in_h=-1)  # past its end → due
    p = await _poll(db_session, s.id)
    a = await _user(db_session, "a@c.com", bal=0)
    b = await _user(db_session, "b@c.com", bal=0)
    await ss.cast_vote(db_session, p.id, a.id, 2)
    await ss.cast_vote(db_session, p.id, b.id, 2)  # option 2 wins
    # E12 scores so top-3 bonus has someone to pay.
    db_session.add(SeasonScore(season_id=s.id, user_id=a.id, points=50))
    await db_session.commit()

    settled = await ss.settle_due_seasons(db_session)
    assert len(settled) == 1

    await db_session.refresh(s)
    assert s.status == "settled"
    assert s.payload_json["poll_results"][0]["winner_idx"] == 2
    await db_session.refresh(p)
    assert p.status == "closed"
    # Rank-1 bonus (200) landed.
    await db_session.refresh(a)
    assert a.soul_coin_balance == 200
    # Finale recap posted.
    recap = (await db_session.execute(
        select(BulletinPost).where(BulletinPost.kind == "digest", BulletinPost.pinned.is_(True))
    )).scalars().all()
    assert any("落幕" in r.title for r in recap)

    # Idempotent: already settled → no second pass.
    assert await ss.settle_due_seasons(db_session) == []


# --------------------------------------------------------------------------- #
# World-view injection                                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_active_season_worldview_injected(db_session):
    from app.services.world_event_service import get_active_events_cached, invalidate_active_cache
    await _season(db_session, world_view="小镇被大雾笼罩，人人自危。")
    invalidate_active_cache()
    events = await get_active_events_cached(db_session)
    assert any(e.get("type") == "season" and "大雾" in e.get("description", "") for e in events)
    invalidate_active_cache()  # don't leak into other tests
