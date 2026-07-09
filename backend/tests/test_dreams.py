"""E2 dreams: generation gates, dream memory, involves-user event, prompt injection."""

from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.resident import Resident
from app.models.memory import Memory


def _mock_client(text):
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text=text)]
    client.messages.create = AsyncMock(return_value=resp)
    return client


async def _resident(db, slug="klaus"):
    r = Resident(slug=slug, name="克劳斯", creator_id="system", district="cafe", status="idle",
                 tile_x=1, tile_y=1, meta_json={"sbti": {"type": "OJBK"}})
    db.add(r)
    await db.commit()
    return r


async def _memories(db, resident_id, n, related_user_id=None):
    now = datetime.now(UTC)
    for i in range(n):
        db.add(Memory(resident_id=resident_id, type="event", content=f"今天的事{i}",
                      importance=0.7, source="chat_resident", created_at=now,
                      related_user_id=(related_user_id if i == 0 else None)))
    await db.commit()


@pytest.mark.anyio
async def test_dream_generated_for_active_resident(db_session):
    from app.services import dream_service as ds
    res = await _resident(db_session)
    await _memories(db_session, res.id, 3)

    with patch.object(ds, "get_client", return_value=_mock_client("我梦见咖啡馆飞上了天。")), \
         patch.object(ds, "record_usage", new_callable=AsyncMock), \
         patch("app.services.dream_service.random.random", return_value=0.1):
        dream = await ds.generate_dream(db_session, res)

    assert dream is not None and dream.type == "dream" and dream.importance == 0.4
    assert dream.source == "reflection" and "咖啡馆" in dream.content


@pytest.mark.anyio
async def test_inactive_resident_no_dream(db_session):
    from app.services import dream_service as ds
    res = await _resident(db_session)
    await _memories(db_session, res.id, 2)  # < 3
    with patch.object(ds, "get_client", return_value=_mock_client("x")), \
         patch.object(ds, "record_usage", new_callable=AsyncMock), \
         patch("app.services.dream_service.random.random", return_value=0.1):
        assert await ds.generate_dream(db_session, res) is None


@pytest.mark.anyio
async def test_probability_gate(db_session):
    from app.services import dream_service as ds
    res = await _resident(db_session)
    await _memories(db_session, res.id, 3)
    with patch.object(ds, "get_client", return_value=_mock_client("x")), \
         patch.object(ds, "record_usage", new_callable=AsyncMock), \
         patch("app.services.dream_service.random.random", return_value=0.9):
        assert await ds.generate_dream(db_session, res) is None


@pytest.mark.anyio
async def test_dream_involving_user_emits(db_session):
    from app.services import dream_service as ds
    res = await _resident(db_session)
    await _memories(db_session, res.id, 3, related_user_id="u1")

    with patch.object(ds, "get_client", return_value=_mock_client("我梦见了一位老朋友。")), \
         patch.object(ds, "record_usage", new_callable=AsyncMock), \
         patch.object(ds, "emit", new_callable=AsyncMock) as emit_mock, \
         patch("app.services.notification_service.manager.is_online", AsyncMock(return_value=False)), \
         patch("app.services.dream_service.random.random", return_value=0.1):
        dream = await ds.generate_dream(db_session, res)

    assert dream.metadata_json["involves_user_id"] == "u1"
    assert any(c.args[1] == "dream_generated" for c in emit_mock.call_args_list)


def test_dream_prompt_injection():
    from app.llm.prompt import assemble_system_prompt
    resident = Resident(slug="r", name="小明", district="cafe", status="idle",
                        tile_x=0, tile_y=0, soul_md="", persona_md="", ability_md="")
    prompt = assemble_system_prompt(resident, recent_dream="我梦见会飞的鱼")
    assert "昨晚你做了个梦" in prompt and "会飞的鱼" in prompt


@pytest.mark.anyio
async def test_get_recent_dream(db_session):
    from app.services.dream_service import get_recent_dream
    res = await _resident(db_session)
    db_session.add(Memory(resident_id=res.id, type="dream", content="梦A", importance=0.4,
                          source="reflection", created_at=datetime.now(UTC)))
    db_session.add(Memory(resident_id=res.id, type="dream", content="旧梦", importance=0.4,
                          source="reflection", created_at=datetime.now(UTC) - timedelta(days=2)))
    await db_session.commit()
    assert await get_recent_dream(db_session, res.id) == "梦A"
