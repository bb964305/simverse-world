"""A5 village daily report: cold-start fallback, compose, idempotency, API."""

from datetime import date, datetime, UTC
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select, func

from app.models.memory import Memory
from app.models.digest import Digest


@pytest.mark.anyio
async def test_cold_start_uses_fallback_no_llm(db_session):
    from app.services import digest_service as ds

    with patch.object(ds, "compose_digest", new_callable=AsyncMock) as compose:
        d = await ds.generate_village_digest(db_session, date(2026, 7, 9))

    compose.assert_not_awaited()  # no material → no LLM
    assert d.scope == "village" and d.user_id == ""
    assert "静悄悄" in d.content_md


@pytest.mark.anyio
async def test_with_material_calls_compose(db_session):
    from app.services import digest_service as ds

    day = datetime.now(UTC).date()
    db_session.add(Memory(
        resident_id="r1", type="event", content="今天大家聊得很开心",
        importance=0.9, source="chat_resident", created_at=datetime.now(UTC),
    ))
    await db_session.commit()

    with patch.object(ds, "compose_digest",
                      AsyncMock(return_value=("今日头条", "# 今日头条\n小镇很热闹"))) as compose:
        d = await ds.generate_village_digest(db_session, day)

    compose.assert_awaited_once()
    assert d.title == "今日头条" and "热闹" in d.content_md
    assert d.stats_json["chat_count"] == 1


@pytest.mark.anyio
async def test_regeneration_is_idempotent(db_session):
    from app.services import digest_service as ds

    day = date(2026, 7, 8)
    with patch.object(ds, "compose_digest", new_callable=AsyncMock):
        d1 = await ds.generate_village_digest(db_session, day)
        d2 = await ds.generate_village_digest(db_session, day)

    assert d1.id == d2.id
    n = (await db_session.execute(select(func.count()).select_from(Digest))).scalar()
    assert n == 1


@pytest.mark.anyio
async def test_digest_api_latest_and_by_date(client, db_session):
    from app.services import digest_service as ds

    day = date(2026, 7, 7)
    with patch.object(ds, "compose_digest", new_callable=AsyncMock):
        await ds.generate_village_digest(db_session, day)

    r = await client.get("/digest/latest")
    assert r.status_code == 200
    assert r.json()["digest"]["date"] == "2026-07-07"

    r2 = await client.get("/digest?date=2026-07-07")
    assert r2.json()["digest"] is not None
    r3 = await client.get("/digest?date=2020-01-01")
    assert r3.json()["digest"] is None


# ── A5→A4: digest pinned to the bulletin board ───────────────────────

@pytest.mark.anyio
async def test_digest_creates_pinned_bulletin_post(db_session):
    from app.services import digest_service as ds
    from app.models.bulletin_post import BulletinPost

    with patch.object(ds, "compose_digest", new_callable=AsyncMock):
        d = await ds.generate_village_digest(db_session, date(2026, 7, 10))

    posts = (await db_session.execute(select(BulletinPost))).scalars().all()
    assert len(posts) == 1
    p = posts[0]
    assert p.kind == "digest" and p.pinned is True
    assert p.title == d.title and p.content_md == d.content_md
    # System post: both author columns stay NULL (board renders「系统」).
    assert p.author_resident_id is None and p.author_user_id is None


@pytest.mark.anyio
async def test_digest_pin_only_latest_stays_pinned(db_session):
    from app.services import digest_service as ds
    from app.models.bulletin_post import BulletinPost

    with patch.object(ds, "compose_digest", new_callable=AsyncMock):
        await ds.generate_village_digest(db_session, date(2026, 7, 10))
        await ds.generate_village_digest(db_session, date(2026, 7, 11))

    pinned = (await db_session.execute(
        select(BulletinPost).where(BulletinPost.pinned.is_(True))
    )).scalars().all()
    assert len(pinned) == 1
    assert "2026-07-11" in pinned[0].title  # the newest one keeps the pin
    total = (await db_session.execute(
        select(func.count()).select_from(BulletinPost)
    )).scalar()
    assert total == 2  # older post survives, just unpinned


@pytest.mark.anyio
async def test_digest_bulletin_post_idempotent_same_day(db_session):
    from app.services import digest_service as ds
    from app.models.bulletin_post import BulletinPost

    with patch.object(ds, "compose_digest", new_callable=AsyncMock):
        await ds.generate_village_digest(db_session, date(2026, 7, 10))
        await ds.generate_village_digest(db_session, date(2026, 7, 10))

    n = (await db_session.execute(
        select(func.count()).select_from(BulletinPost)
    )).scalar()
    assert n == 1  # regenerating the same day does not repost
