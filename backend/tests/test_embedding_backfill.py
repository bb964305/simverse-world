import pytest
from unittest.mock import patch

from sqlalchemy import select

from app.models.memory import Memory
from app.models.resident import Resident
from app.tasks.embedding_backfill import (
    backfill_missing_embeddings,
    cleanup_zero_embeddings,
)

DIM = 1024


@pytest.fixture
async def backfill_resident(db_session):
    r = Resident(
        id="backfill-res",
        slug="backfill-res",
        name="BackfillRes",
        district="engineering",
        status="idle",
        ability_md="",
        persona_md="",
        soul_md="",
        creator_id="c1",
    )
    db_session.add(r)
    await db_session.commit()
    return r


async def _add_memory(db, resident_id, content, embedding):
    mem = Memory(
        resident_id=resident_id,
        type="event",
        content=content,
        importance=0.5,
        source="chat_player",
        embedding=embedding,
    )
    db.add(mem)
    await db.commit()
    return mem


@pytest.mark.anyio
async def test_cleanup_zero_embeddings_nulls_poison_rows(db_session, backfill_resident):
    zero_mem = await _add_memory(db_session, "backfill-res", "poisoned", [0.0] * DIM)
    good_mem = await _add_memory(db_session, "backfill-res", "healthy", [0.1] * DIM)

    cleaned = await cleanup_zero_embeddings(db_session)

    assert cleaned == 1
    await db_session.refresh(zero_mem)
    await db_session.refresh(good_mem)
    assert zero_mem.embedding is None
    assert good_mem.embedding == [0.1] * DIM


@pytest.mark.anyio
async def test_backfill_recomputes_null_embeddings(db_session, backfill_resident):
    null_mem = await _add_memory(db_session, "backfill-res", "needs embedding", None)
    good_mem = await _add_memory(db_session, "backfill-res", "already done", [0.2] * DIM)

    async def fake_embed(text):
        return [0.5] * DIM

    with patch("app.tasks.embedding_backfill.generate_embedding", side_effect=fake_embed):
        fixed = await backfill_missing_embeddings(db_session)

    assert fixed == 1
    await db_session.refresh(null_mem)
    await db_session.refresh(good_mem)
    assert null_mem.embedding == [0.5] * DIM
    assert good_mem.embedding == [0.2] * DIM


@pytest.mark.anyio
async def test_backfill_keeps_null_when_ollama_still_down(db_session, backfill_resident):
    null_mem = await _add_memory(db_session, "backfill-res", "still failing", None)

    async def failing_embed(text):
        return None

    with patch("app.tasks.embedding_backfill.generate_embedding", side_effect=failing_embed):
        fixed = await backfill_missing_embeddings(db_session)

    assert fixed == 0
    await db_session.refresh(null_mem)
    assert null_mem.embedding is None


@pytest.mark.anyio
async def test_backfill_only_touches_event_memories(db_session, backfill_resident):
    rel = Memory(
        resident_id="backfill-res",
        type="relationship",
        content="knows someone",
        importance=0.5,
        source="chat_player",
        embedding=None,
    )
    db_session.add(rel)
    await db_session.commit()

    called = 0

    async def fake_embed(text):
        nonlocal called
        called += 1
        return [0.5] * DIM

    with patch("app.tasks.embedding_backfill.generate_embedding", side_effect=fake_embed):
        fixed = await backfill_missing_embeddings(db_session)

    assert fixed == 0
    assert called == 0
    result = await db_session.execute(select(Memory).where(Memory.type == "relationship"))
    assert result.scalar_one().embedding is None
