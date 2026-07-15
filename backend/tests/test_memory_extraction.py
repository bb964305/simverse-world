import pytest
import json
from unittest.mock import AsyncMock, patch
from app.models.resident import Resident
from app.models.memory import Memory
from app.memory.service import MemoryService


@pytest.fixture
async def resident(db_session):
    r = Resident(
        id="ext-test-res",
        slug="ext-test-res",
        name="ExtractResident",
        district="engineering",
        status="idle",
        ability_md="Python expert",
        persona_md="Thoughtful and quiet",
        soul_md="Seeks truth",
        creator_id="test-user",
        meta_json={"sbti": {"type": "THIN-K", "type_name": "思考者", "dimensions": {
            "S1": "H", "S2": "H", "S3": "L",
            "E1": "H", "E2": "M", "E3": "H",
            "A1": "M", "A2": "L", "A3": "H",
            "Ac1": "M", "Ac2": "H", "Ac3": "M",
            "So1": "L", "So2": "H", "So3": "H",
        }}},
    )
    db_session.add(r)
    await db_session.commit()
    return r


def _mock_llm_response(content: str) -> str:
    """Return text string as llm_chat() now returns text directly."""
    return content


@pytest.mark.anyio
async def test_extract_events_from_conversation(db_session, resident):
    svc = MemoryService(db_session)

    llm_response = json.dumps({
        "memories": [
            {"content": "Discussed Python async patterns", "importance": 0.6},
            {"content": "Visitor shared frustration about debugging", "importance": 0.5},
        ]
    })

    with patch("app.memory.service.llm_chat", new=AsyncMock(return_value=_mock_llm_response(llm_response))):
        with patch("app.memory.service.generate_embedding", return_value=[0.1] * 1024):
            memories = await svc.extract_events(
                resident=resident,
                other_name="Player1",
                conversation_text="Player1: How do I use async?\nExtractResident: Let me explain...",
            )

    assert len(memories) == 2
    assert memories[0].type == "event"
    assert memories[0].source == "chat_player"
    assert memories[0].embedding is not None


@pytest.mark.anyio
async def test_extract_events_handles_llm_failure(db_session, resident):
    svc = MemoryService(db_session)

    with patch("app.memory.service.llm_chat", new=AsyncMock(side_effect=Exception("LLM down"))):
        memories = await svc.extract_events(
            resident=resident,
            other_name="Player1",
            conversation_text="Hello!",
        )

    assert memories == []


@pytest.mark.anyio
async def test_update_relationship_via_llm(db_session, resident):
    svc = MemoryService(db_session)

    llm_response = json.dumps({
        "content": "Player1 is a curious beginner interested in Python async",
        "importance": 0.6,
        "metadata": {"affinity": 0.4, "trust": 0.5, "tags": ["beginner", "curious"]},
    })

    with patch("app.memory.service.llm_chat", new=AsyncMock(return_value=_mock_llm_response(llm_response))):
        rel = await svc.update_relationship_via_llm(
            resident=resident,
            other_name="Player1",
            user_id="user-1",
            event_summaries=["Discussed Python async patterns"],
        )

    assert rel.content == "Player1 is a curious beginner interested in Python async"
    assert rel.metadata_json["affinity"] == 0.4


@pytest.mark.anyio
async def test_trigger_reflection(db_session, resident):
    svc = MemoryService(db_session)

    # Seed some event and relationship memories
    for i in range(5):
        await svc.add_memory(resident.id, "event", f"Event {i}", 0.5, "chat_player")
    await svc.add_memory(
        resident.id, "relationship", "A friendly visitor",
        0.5, "chat_player", related_user_id="user-1",
    )

    llm_response = json.dumps({
        "reflections": [
            {"content": "I notice visitors often ask about async programming", "importance": 0.7},
            {"content": "People seem genuinely interested in learning", "importance": 0.6},
        ]
    })

    with patch("app.memory.service.llm_chat", new=AsyncMock(return_value=_mock_llm_response(llm_response))):
        reflections = await svc.generate_reflections(resident=resident)

    assert len(reflections) == 2
    assert reflections[0].type == "reflection"
    assert reflections[0].source == "reflection"


@pytest.mark.anyio
async def test_resolve_resident_mentions(db_session):
    from app.models.resident import Resident
    from app.services.resident_service import resolve_resident_mentions

    r1 = Resident(slug="klaus", name="克劳斯", persona_md="x", creator_id="test-user")
    r2 = Resident(slug="mei", name="梅", persona_md="x", creator_id="test-user")
    db_session.add_all([r1, r2])
    await db_session.commit()

    mapping = await resolve_resident_mentions(db_session, ["克劳斯", "mei", "不存在的人"])
    assert mapping["克劳斯"] == r1.id
    assert mapping["mei"] == r2.id
    assert "不存在的人" not in mapping


@pytest.mark.anyio
async def test_extract_events_sets_related_resident(db_session, resident):
    from app.models.resident import Resident
    third = Resident(slug="adam", name="亚当", persona_md="x", creator_id="test-user")
    db_session.add(third)
    await db_session.commit()

    llm_response = json.dumps({"memories": [
        {"content": "聊到了亚当在广场发呆的事", "importance": 0.7,
         "mentioned_resident": "亚当"},
        {"content": "玩家喜欢喝咖啡", "importance": 0.4},
    ]})
    with patch("app.memory.service.llm_chat", new=AsyncMock(return_value=llm_response)):
        with patch("app.memory.service.generate_embedding", return_value=[0.1] * 1024):
            svc = MemoryService(db_session)
            memories = await svc.extract_events(
                resident=resident, other_name="Player1", conversation_text="...")

    by_content = {m.content[:6]: m for m in memories}
    assert by_content["聊到了亚当在"].related_resident_id == third.id
    assert by_content["玩家喜欢喝咖"].related_resident_id is None


@pytest.mark.anyio
async def test_wrapup_sets_related_resident_default_partner(db_session, resident):
    from app.models.resident import Resident
    partner = Resident(slug="mei", name="梅", persona_md="x", creator_id="test-user")
    third = Resident(slug="adam", name="亚当", persona_md="x", creator_id="test-user")
    db_session.add_all([partner, third])
    await db_session.commit()

    wrapup_json = json.dumps({
        "initiator": {"memories": [
            {"content": "梅提到亚当总在广场发呆", "importance": 0.7, "mentioned_resident": "亚当"},
            {"content": "和梅聊得很愉快", "importance": 0.5},
        ], "relationship": {"content": "对梅有好感", "importance": 0.5,
                            "metadata": {"affinity": 1, "trust": 1, "tags": []}}},
        "target": {"memories": [], "relationship": None},
        "summary": "s", "mood": "neutral",
    })
    with patch("app.memory.service.llm_chat", new=AsyncMock(return_value=wrapup_json)):
        with patch("app.memory.service.generate_embedding", return_value=[0.1] * 1024):
            svc = MemoryService(db_session)
            await svc.process_chat_wrapup(resident, partner, "对话全文")

    from sqlalchemy import select
    from app.models.memory import Memory
    rows = (await db_session.execute(select(Memory).where(
        Memory.resident_id == resident.id, Memory.type == "event"))).scalars().all()
    by_content = {m.content[:5]: m for m in rows}
    assert by_content["梅提到亚当"].related_resident_id == third.id   # 显式提及 → 第三方
    assert by_content["和梅聊得很"].related_resident_id == partner.id  # 默认 → 对话对象


@pytest.mark.anyio
async def test_wrapup_memory_feeds_gossip(db_session, resident):
    """wrapup 写出的高重要度记忆（related=partner）能被 maybe_gossip 选中传给第三人。"""
    from app.models.resident import Resident
    from app.services import gossip_service as gs

    partner = Resident(slug="mei", name="梅", persona_md="x", creator_id="test-user")
    listener = Resident(slug="adam", name="亚当", persona_md="x", creator_id="test-user")
    db_session.add_all([partner, listener])
    await db_session.commit()

    wrapup_json = json.dumps({
        "initiator": {"memories": [
            {"content": "梅答应帮全村修钟楼", "importance": 0.8}],
            "relationship": None},
        "target": {"memories": [], "relationship": None},
        "summary": "s", "mood": "neutral",
    })
    with patch("app.memory.service.llm_chat", new=AsyncMock(return_value=wrapup_json)):
        with patch("app.memory.service.generate_embedding", return_value=[0.1] * 1024):
            await MemoryService(db_session).process_chat_wrapup(resident, partner, "text")

    with patch("app.services.gossip_service.random.random", side_effect=[0.1, 0.9]):
        g = await gs.maybe_gossip(db_session, resident, listener)
    assert g is not None and g.related_resident_id == partner.id
