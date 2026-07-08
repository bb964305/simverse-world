import pytest
import json
from unittest.mock import AsyncMock, patch
from sqlalchemy import select
from app.models.resident import Resident
from app.models.memory import Memory
from app.agent.chat import resident_chat, _chat_cooldowns


@pytest.fixture
async def chat_pair(db_session):
    initiator = Resident(
        id="chat-init",
        slug="chat-init",
        name="Initiator",
        district="engineering",
        status="idle",
        ability_md="Likes talking",
        persona_md="Outgoing",
        soul_md="Social",
        creator_id="c1",
        meta_json={"sbti": {"type": "GOGO", "type_name": "行者", "dimensions": {
            "So1": "H", "So2": "M", "So3": "H",
            "S1": "H", "S2": "H", "S3": "M",
            "E1": "H", "E2": "M", "E3": "H",
            "A1": "M", "A2": "M", "A3": "H",
            "Ac1": "H", "Ac2": "H", "Ac3": "H",
        }}},
    )
    target = Resident(
        id="chat-tgt",
        slug="chat-tgt",
        name="Target",
        district="engineering",
        status="idle",
        ability_md="Good listener",
        persona_md="Reflective",
        soul_md="Curious",
        creator_id="c1",
        meta_json={"sbti": {"type": "THIN-K", "type_name": "思考者", "dimensions": {
            "So1": "L", "So2": "H", "So3": "H",
            "S1": "H", "S2": "H", "S3": "L",
            "E1": "H", "E2": "M", "E3": "H",
            "A1": "M", "A2": "L", "A3": "H",
            "Ac1": "M", "Ac2": "H", "Ac3": "M",
        }}},
    )
    db_session.add(initiator)
    db_session.add(target)
    await db_session.commit()
    return initiator, target


def _mock_llm_text(text: str) -> str:
    return text


@pytest.mark.anyio
async def test_resident_chat_creates_memories(db_session, chat_pair):
    initiator, target = chat_pair
    _chat_cooldowns.clear()

    dialog_responses = [
        "你好啊，今天天气不错！",       # turn 1: initiator opens
        "是啊，你去哪里玩了吗？",        # turn 2: target replies
        "我刚从工程区回来，很有意思。",  # turn 3: initiator
    ]
    # Wrap-up is now ONE merged call (E-04/E-05) producing both residents'
    # memories + relationships + summary in a single JSON.
    wrapup_response = json.dumps({
        "initiator": {
            "memories": [{"content": "和 Target 聊了天气和工程区", "importance": 0.5}],
            "relationship": {"content": "Target 是个好相处的人", "importance": 0.5,
                             "metadata": {"affinity": 0.4, "trust": 0.5, "tags": ["friendly"]}},
        },
        "target": {
            "memories": [{"content": "和 Initiator 聊了天气和工程区", "importance": 0.5}],
            "relationship": {"content": "Initiator 很健谈", "importance": 0.5,
                             "metadata": {"affinity": 0.4, "trust": 0.5, "tags": ["talkative"]}},
        },
        "summary": "Initiator 和 Target 聊了天气和工程区的趣事",
        "mood": "positive",
    })

    call_idx = 0
    # Order: 3 dialog turns, then 1 merged wrap-up call.
    all_responses = dialog_responses + [wrapup_response]

    async def side_effect(*args, **kwargs):
        nonlocal call_idx
        resp = all_responses[min(call_idx, len(all_responses) - 1)]
        call_idx += 1
        return resp

    with patch("app.agent.chat.llm_chat", side_effect=side_effect), \
         patch("app.memory.service.llm_chat", side_effect=side_effect):

        result = await resident_chat(db_session, initiator, target, max_turns=3)

    assert "summary" in result
    assert len(result["summary"]) > 0
    assert "mood" in result

    # Both residents should return to idle
    await db_session.refresh(initiator)
    await db_session.refresh(target)
    assert initiator.status == "idle"
    assert target.status == "idle"

    # Memories should be created for both
    init_mems = (await db_session.execute(
        select(Memory).where(Memory.resident_id == initiator.id, Memory.type == "event")
    )).scalars().all()
    tgt_mems = (await db_session.execute(
        select(Memory).where(Memory.resident_id == target.id, Memory.type == "event")
    )).scalars().all()
    assert len(init_mems) >= 1
    assert len(tgt_mems) >= 1


def _wrapup_json():
    return json.dumps({
        "initiator": {"memories": [{"content": "初始者记得聊了A", "importance": 0.5}],
                      "relationship": {"content": "对方不错", "importance": 0.5, "metadata": {}}},
        "target": {"memories": [{"content": "目标记得聊了B", "importance": 0.5}],
                   "relationship": {"content": "对方健谈", "importance": 0.5, "metadata": {}}},
        "summary": "两人聊得开心", "mood": "positive",
    })


@pytest.mark.anyio
async def test_process_chat_wrapup_persists_and_summarizes(db_session, chat_pair):
    """The merged wrap-up persists both sides' memories + relationships and
    returns the summary/mood (E-04)."""
    from app.memory.service import MemoryService
    initiator, target = chat_pair
    svc = MemoryService(db_session)
    with patch("app.memory.service.llm_chat", new=AsyncMock(return_value=_wrapup_json())) as m:
        result = await svc.process_chat_wrapup(initiator, target, "对白全文")
    assert result["summary"] == "两人聊得开心"
    assert result["mood"] == "positive"
    m.assert_awaited_once()  # exactly ONE wrap-up call replaced the old five
    for rid in (initiator.id, target.id):
        events = (await db_session.execute(
            select(Memory).where(Memory.resident_id == rid, Memory.type == "event")
        )).scalars().all()
        rels = (await db_session.execute(
            select(Memory).where(Memory.resident_id == rid, Memory.type == "relationship")
        )).scalars().all()
        assert len(events) >= 1 and len(rels) >= 1


@pytest.mark.anyio
async def test_process_chat_wrapup_retries_once_on_parse_failure(db_session, chat_pair):
    """A first unparseable reply triggers exactly one retry (E-05)."""
    from app.memory.service import MemoryService
    initiator, target = chat_pair
    svc = MemoryService(db_session)
    side = AsyncMock(side_effect=["这不是JSON", _wrapup_json()])
    with patch("app.memory.service.llm_chat", new=side):
        result = await svc.process_chat_wrapup(initiator, target, "对白")
    assert side.await_count == 2
    assert result["mood"] == "positive"
    events = (await db_session.execute(
        select(Memory).where(Memory.resident_id == initiator.id, Memory.type == "event")
    )).scalars().all()
    assert len(events) >= 1


@pytest.mark.anyio
async def test_process_chat_wrapup_falls_back_when_both_fail(db_session, chat_pair):
    """Both attempts unparseable -> generic summary, no memories (no blank screen)."""
    from app.memory.service import MemoryService
    initiator, target = chat_pair
    svc = MemoryService(db_session)
    side = AsyncMock(side_effect=["nope", "still nope"])
    with patch("app.memory.service.llm_chat", new=side):
        result = await svc.process_chat_wrapup(initiator, target, "对白")
    assert side.await_count == 2
    assert result["mood"] == "neutral"
    assert result["summary"]
    events = (await db_session.execute(
        select(Memory).where(Memory.resident_id == initiator.id, Memory.type == "event")
    )).scalars().all()
    assert len(events) == 0


@pytest.mark.anyio
async def test_resident_chat_cooldown(db_session, chat_pair):
    initiator, target = chat_pair
    _chat_cooldowns.clear()

    # Manually set a fresh cooldown for this pair
    pair_key = tuple(sorted([initiator.id, target.id]))
    import time
    _chat_cooldowns[pair_key] = time.time()  # just set, not expired

    result = await resident_chat(db_session, initiator, target)

    # Should return None/empty dict if on cooldown
    assert result is None or result.get("skipped") is True


@pytest.mark.anyio
async def test_resident_chat_busy_target_skipped(db_session, chat_pair):
    initiator, target = chat_pair
    _chat_cooldowns.clear()
    target.status = "chatting"
    await db_session.commit()

    result = await resident_chat(db_session, initiator, target)

    assert result is None or result.get("skipped") is True
