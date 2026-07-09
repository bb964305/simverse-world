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
