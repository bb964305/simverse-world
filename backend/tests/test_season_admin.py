"""E7 赛季写端：admin 开季 / 结季、自动开季、列表路由。

全仓 `Season(` 的构造此前只出现在类定义与测试里 —— 没有任何生产代码路径
会创建赛季行。后果链：_active_season_id() 恒 None → add_points() 第一行
`if not season_id: return 0` → 所有经 season_scorer 上报的积分全部静默丢弃。
"""
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.season import Season
from app.models.user import User
from app.services import script_service as ss
from app.services import season_service
from app.services.auth_service import create_token


async def _admin(db):
    u = User(name="admin", email="season-admin@t.com", is_admin=True, is_banned=False)
    db.add(u)
    await db.commit()
    return u


def _hdr(user):
    return {"Authorization": f"Bearer {create_token(user.id)}"}


@pytest.fixture(autouse=True)
def _clear_season_cache():
    """_active_season_id 有 60s 进程内缓存，跨测试会串味。"""
    season_service._invalidate_active()
    yield
    season_service._invalidate_active()


@pytest.mark.anyio
async def test_admin_can_open_a_season(client, db_session):
    admin = await _admin(db_session)
    resp = await client.post("/admin/seasons", headers=_hdr(admin), json={
        "title": "谜案季", "theme": "小镇疑云", "days": 7,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "谜案季" and body["status"] == "active"

    row = (await db_session.execute(select(Season))).scalars().one()
    assert row.status == "active" and row.title == "谜案季"


@pytest.mark.anyio
async def test_opening_a_season_invalidates_the_active_cache(client, db_session):
    """不打掉 60s 缓存，新赛季最长 1 分钟不可见，记分继续丢。"""
    admin = await _admin(db_session)
    # 先把缓存烧成 "无赛季"
    assert await season_service._active_season_id(db_session) is None

    await client.post("/admin/seasons", headers=_hdr(admin),
                      json={"title": "新季", "theme": "", "days": 7})

    assert await season_service._active_season_id(db_session) is not None


@pytest.mark.anyio
async def test_opening_refuses_while_another_season_is_active(client, db_session):
    admin = await _admin(db_session)
    db_session.add(Season(title="在办季", status="active",
                          starts_at=datetime.now(UTC) - timedelta(days=1),
                          ends_at=datetime.now(UTC) + timedelta(days=6)))
    await db_session.commit()

    resp = await client.post("/admin/seasons", headers=_hdr(admin),
                             json={"title": "抢跑季", "theme": "", "days": 7})
    assert resp.status_code == 400
    assert "active" in resp.json()["detail"]


@pytest.mark.anyio
async def test_admin_can_settle_a_season(client, db_session):
    admin = await _admin(db_session)
    s = Season(title="待结季", status="active",
               starts_at=datetime.now(UTC) - timedelta(days=8),
               ends_at=datetime.now(UTC) - timedelta(days=1))
    db_session.add(s)
    await db_session.commit()

    resp = await client.post(f"/admin/seasons/{s.id}/settle", headers=_hdr(admin))
    assert resp.status_code == 200
    await db_session.refresh(s)
    assert s.status == "settled"


@pytest.mark.anyio
async def test_settle_404s_on_an_unknown_season(client, db_session):
    admin = await _admin(db_session)
    resp = await client.post("/admin/seasons/nope/settle", headers=_hdr(admin))
    assert resp.status_code == 404


@pytest.mark.anyio
async def test_seasons_list_endpoint_is_public(client, db_session):
    db_session.add_all([
        Season(title="第一季", status="settled",
               starts_at=datetime.now(UTC) - timedelta(days=30),
               ends_at=datetime.now(UTC) - timedelta(days=16)),
        Season(title="第二季", status="active",
               starts_at=datetime.now(UTC) - timedelta(days=2),
               ends_at=datetime.now(UTC) + timedelta(days=12)),
    ])
    await db_session.commit()

    resp = await client.get("/seasons")
    assert resp.status_code == 200
    titles = [s["title"] for s in resp.json()["seasons"]]
    assert titles == ["第二季", "第一季"]  # 新的在前


@pytest.mark.anyio
async def test_ensure_active_season_opens_one_when_none_exists(db_session):
    s = await ss.ensure_active_season(db_session)
    assert s is not None and s.status == "active"
    span = (s.ends_at - s.starts_at).days
    assert span == settings.season_length_days


@pytest.mark.anyio
async def test_ensure_active_season_is_a_noop_when_one_is_running(db_session):
    db_session.add(Season(title="在办季", status="active",
                          starts_at=datetime.now(UTC) - timedelta(days=1),
                          ends_at=datetime.now(UTC) + timedelta(days=6)))
    await db_session.commit()

    assert await ss.ensure_active_season(db_session) is None
    n = len((await db_session.execute(select(Season))).scalars().all())
    assert n == 1


@pytest.mark.anyio
async def test_ensure_active_season_respects_the_gate(db_session, monkeypatch):
    monkeypatch.setattr(settings, "season_auto_open", False)
    assert await ss.ensure_active_season(db_session) is None
    assert (await db_session.execute(select(Season))).scalars().all() == []


@pytest.mark.anyio
async def test_points_actually_land_once_a_season_exists(db_engine, db_session,
                                                         monkeypatch):
    """E7 的真正判据：开季之后 add_points 不再静默丢弃。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
    from app.models.season import SeasonScore

    await ss.ensure_active_season(db_session)
    season_service._invalidate_active()

    # add_points 自己开 session（不收 db），照 test_seasons.py:14-21 的既有姿势
    # 注入测试 engine —— 这是唯一能让它在测试里工作的方式。
    factory = async_sessionmaker(db_engine, class_=AsyncSession,
                                 expire_on_commit=False)
    monkeypatch.setattr(season_service, "async_session", factory)
    assert await season_service.add_points("u1", 30, "chat") == 30

    score = (await db_session.execute(
        select(SeasonScore).where(SeasonScore.user_id == "u1"))).scalar_one()
    assert score.points == 30


def test_event_cron_wires_auto_season_opening():
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "app" / "tasks"
           / "event_cron.py").read_text(encoding="utf-8")
    assert "ensure_active_season" in src
