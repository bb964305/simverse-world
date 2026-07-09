"""E12 season scoring: add_points daily cap, scorer, leaderboard, settlement."""

from datetime import datetime, timedelta, UTC
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models.user import User
from app.models.season import Season, SeasonScore


@pytest.fixture
def season_env(db_engine):
    from app.services import season_service as ss
    ss._invalidate_active()
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch.object(ss, "async_session", factory):
        yield ss
    ss._invalidate_active()


async def _season(db, status="active"):
    s = Season(title="第一季", theme="神秘失踪案", status=status,
               starts_at=datetime.now(UTC) - timedelta(days=1), ends_at=datetime.now(UTC) + timedelta(days=6))
    db.add(s)
    await db.commit()
    return s


@pytest.mark.anyio
async def test_add_points_daily_cap(db_session, season_env):
    ss = season_env
    await _season(db_session)
    assert await ss.add_points("u1", 30, "chat") == 30
    assert await ss.add_points("u1", 80, "explore") == 70  # capped to 100 total
    assert await ss.add_points("u1", 10, "x") == 0

    score = (await db_session.execute(select(SeasonScore).where(SeasonScore.user_id == "u1"))).scalar_one()
    assert score.points == 100 and score.breakdown_json["chat"] == 30 and score.breakdown_json["explore"] == 70


@pytest.mark.anyio
async def test_no_active_season_no_score(db_session, season_env):
    ss = season_env
    await _season(db_session, status="voting")  # not active
    assert await ss.add_points("u1", 30, "chat") == 0


@pytest.mark.anyio
async def test_scorer_chat_first_five(db_session, season_env):
    from app.events.bus import emit
    await _season(db_session)
    for _ in range(6):
        await emit(None, "chat_completed", user_id="u1", resident_id="r", turns=1)
    score = (await db_session.execute(select(SeasonScore).where(SeasonScore.user_id == "u1"))).scalar_one()
    assert score.points == 25  # first 5 chats × 5


@pytest.mark.anyio
async def test_leaderboard_around_me(db_session):
    from app.services.season_service import leaderboard
    season = await _season(db_session)
    for uid, pts in [("u1", 100), ("u2", 60), ("u3", 30), ("u4", 10)]:
        db_session.add(SeasonScore(season_id=season.id, user_id=uid, points=pts, updated_at=datetime.now(UTC)))
    await db_session.commit()

    lb = await leaderboard(db_session, season.id, user_id="u3", around_me=True)
    assert lb["top"][0]["user_id"] == "u1" and lb["top"][0]["rank"] == 1
    assert lb["around_me"]["my_rank"] == 3


@pytest.mark.anyio
async def test_settle_idempotent_with_bonus(db_session):
    from app.services.season_service import settle_season
    season = await _season(db_session)
    winner = User(name="w", email="w@s.com", soul_coin_balance=0)
    db_session.add(winner)
    await db_session.commit()
    db_session.add(SeasonScore(season_id=season.id, user_id=winner.id, points=500, updated_at=datetime.now(UTC)))
    await db_session.commit()

    p1 = await settle_season(db_session, season)
    assert p1["settled"] and p1["final_ranks"][0]["user_id"] == winner.id
    assert season.status == "settled"
    await db_session.refresh(winner)
    assert winner.soul_coin_balance == 200  # rank-1 bonus

    p2 = await settle_season(db_session, season)  # idempotent — no double bonus
    await db_session.refresh(winner)
    assert winner.soul_coin_balance == 200
