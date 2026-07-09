"""A4 resident creations: journal publishing, tip effect (share + patron), feed API."""

from unittest.mock import patch

import pytest
from sqlalchemy import select, func

from app.models.user import User
from app.models.resident import Resident
from app.models.memory import Memory
from app.models.bulletin_post import BulletinPost


async def _user(db, email, bal=0):
    u = User(name="u", email=email, soul_coin_balance=bal)
    db.add(u)
    await db.commit()
    return u


async def _resident(db, slug, creator_id="system"):
    r = Resident(slug=slug, name=slug, creator_id=creator_id, district="cafe", status="idle", tile_x=1, tile_y=1)
    db.add(r)
    await db.commit()
    return r


@pytest.mark.anyio
async def test_journal_publishes_once_per_day(db_session):
    from app.services import bulletin_service as bs
    res = await _resident(db_session, "klaus")
    db_session.add(Memory(resident_id=res.id, type="event", content="今天遇到了有趣的人", importance=0.5, source="chat_resident"))
    await db_session.commit()

    with patch("app.services.bulletin_service.random.random", return_value=0.1):
        p1 = await bs.maybe_create_journal_post(db_session, res)
        p2 = await bs.maybe_create_journal_post(db_session, res)  # already posted today

    assert p1 is not None and "有趣的人" in p1.content_md and p1.kind == "journal"
    assert p2 is None
    n = (await db_session.execute(select(func.count()).select_from(BulletinPost))).scalar()
    assert n == 1


@pytest.mark.anyio
async def test_journal_probability_gate(db_session):
    from app.services import bulletin_service as bs
    res = await _resident(db_session, "klaus")
    with patch("app.services.bulletin_service.random.random", return_value=0.9):
        assert await bs.maybe_create_journal_post(db_session, res) is None


@pytest.mark.anyio
async def test_tip_effect_shares_with_creator(db_session):
    from app.services.shop_service import purchase, seed_items
    from app.services import bulletin_service as bs
    await seed_items(db_session)
    creator = await _user(db_session, "creator@a4.com", bal=0)
    buyer = await _user(db_session, "buyer@a4.com", bal=100)
    res = await _resident(db_session, "klaus", creator_id=creator.id)
    post = await bs.create_post(db_session, "journal", "随笔", "内容", author_resident_id=res.id)

    result = await purchase(db_session, buyer.id, "tip_5sc", 1, {"post_id": post.id})
    assert result["effect"]["tips_sc"] == 5
    assert result["effect"]["creator_share"] == 4  # 80% of 5

    await db_session.refresh(post)
    assert post.tips_sc == 5
    await db_session.refresh(creator)
    assert creator.soul_coin_balance == 4


@pytest.mark.anyio
async def test_bulletin_posts_feed_api(client, db_session):
    from app.services import bulletin_service as bs
    res = await _resident(db_session, "klaus")
    await bs.create_post(db_session, "journal", "第一篇", "abc", author_resident_id=res.id)

    resp = await client.get("/bulletin/posts")
    assert resp.status_code == 200
    posts = resp.json()["posts"]
    assert len(posts) == 1 and posts[0]["title"] == "第一篇" and posts[0]["author_name"] == "klaus"
