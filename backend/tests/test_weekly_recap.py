"""E14 personal weekly recap: idempotent (1 LLM/week), cold-start fallback, tag."""

from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select, func

from app.models.user import User
from app.models.resident import Resident
from app.models.conversation import Conversation
from app.models.digest import Digest


def _mock_client(text="这一周你过得很充实。"):
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    client.messages.create = AsyncMock(return_value=resp)
    return client


async def _user(db, email):
    u = User(name="u", email=email)
    db.add(u)
    await db.commit()
    return u


async def _conv(db, user_id, resident_id, turns=5):
    c = Conversation(user_id=user_id, resident_id=resident_id, turns=turns, started_at=datetime.now(UTC))
    db.add(c)
    await db.commit()
    return c


@pytest.mark.anyio
async def test_cold_start_no_llm(db_session):
    from app.services import digest_service as ds
    user = await _user(db_session, "wk1@t.com")
    await _conv(db_session, user.id, "r1")  # only 1 chat < 2

    called = []

    async def _fake_chat(*a, **kw):
        called.append(1)
        return "不该被调用"

    with patch.object(ds, "llm_chat", _fake_chat):
        d = await ds.generate_weekly_recap(db_session, user.id)

    assert not called
    assert d.scope == "personal" and "太安静" in d.content_md


@pytest.mark.anyio
async def test_recap_one_llm_call_per_week(db_session):
    from app.services import digest_service as ds
    user = await _user(db_session, "wk2@t.com")
    await _conv(db_session, user.id, "r1")
    await _conv(db_session, user.id, "r2")
    await _conv(db_session, user.id, "r3")

    calls = []

    async def _fake_chat(system_prompt, messages, model=None, max_tokens=None, **kw):
        calls.append({"system": system_prompt, "messages": messages, "kw": kw})
        return "你和三位居民建立了联系。"

    with patch.object(ds, "llm_chat", _fake_chat):
        d1 = await ds.generate_weekly_recap(db_session, user.id)
        d2 = await ds.generate_weekly_recap(db_session, user.id)  # idempotent

    assert d1.id == d2.id
    assert len(calls) == 1  # only one LLM call this week
    n = (await db_session.execute(select(func.count()).select_from(Digest))).scalar()
    assert n == 1
    assert d1.stats_json["distinct_residents"] == 3 and d1.stats_json["tag"]


def test_personality_tag_reproducible():
    from app.services.digest_service import _personality_tag
    assert _personality_tag(0, 0, 0) == "沉睡者"
    assert _personality_tag(3, 5, 1) == "社交名流"
    assert _personality_tag(2, 1, 6) == "城市漫游者"
    assert _personality_tag(12, 2, 0) == "健谈者"
