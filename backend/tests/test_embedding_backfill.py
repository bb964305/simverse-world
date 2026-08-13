from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.models.memory import Memory
from app.models.resident import Resident
from app.config import Settings, settings
from app.tasks.embedding_backfill import (
    backfill_missing_embeddings,
    cleanup_zero_embeddings,
)

DIM = 1024


def _env_values(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.split("#", 1)[0].strip()
    return values


def test_backfill_canary_defaults_are_documented_in_both_env_templates():
    expected = {
        "EMBEDDING_BACKFILL_BATCH_SIZE": "100",
        "EMBEDDING_BACKFILL_INTERVAL_SECONDS": "600",
        "EMBEDDING_BACKFILL_REQUEST_SIZE": "50",
    }
    fields = Settings.model_fields
    assert fields["embedding_backfill_batch_size"].default == 100
    assert fields["embedding_backfill_interval_seconds"].default == 600
    assert fields["embedding_backfill_request_size"].default == 50

    backend = Path(__file__).resolve().parents[1]
    repo = backend.parent
    for template in (backend / ".env.example", repo / "deploy/backend/.env.example"):
        values = _env_values(template)
        assert {key: values.get(key) for key in expected} == expected


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

    async def fake_embed(texts):
        return [[0.5] * DIM for _ in texts]

    with patch(
        "app.tasks.embedding_backfill.generate_embeddings_batch",
        side_effect=fake_embed,
    ):
        fixed = await backfill_missing_embeddings(db_session)

    assert fixed == 1
    await db_session.refresh(null_mem)
    await db_session.refresh(good_mem)
    assert null_mem.embedding == [0.5] * DIM
    assert good_mem.embedding == [0.2] * DIM


@pytest.mark.anyio
async def test_backfill_keeps_null_when_ollama_still_down(db_session, backfill_resident):
    null_mem = await _add_memory(db_session, "backfill-res", "still failing", None)

    async def failing_embed(texts):
        return [None] * len(texts)

    with patch(
        "app.tasks.embedding_backfill.generate_embeddings_batch",
        side_effect=failing_embed,
    ):
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

    async def fake_embed(texts):
        nonlocal called
        called += len(texts)
        return [[0.5] * DIM for _ in texts]

    with patch(
        "app.tasks.embedding_backfill.generate_embeddings_batch",
        side_effect=fake_embed,
    ):
        fixed = await backfill_missing_embeddings(db_session)

    assert fixed == 0
    assert called == 0
    result = await db_session.execute(select(Memory).where(Memory.type == "relationship"))
    assert result.scalar_one().embedding is None


@pytest.mark.anyio
async def test_backfill_is_fifo_and_chunks_provider_requests(
    db_session, backfill_resident, monkeypatch
):
    now = datetime.now(UTC)
    memories = [
        Memory(
            resident_id="backfill-res",
            type="event",
            content=content,
            importance=0.5,
            source="agent_action",
            embedding=None,
            created_at=created_at,
        )
        for content, created_at in (
            ("newest", now),
            ("oldest", now - timedelta(hours=2)),
            ("middle", now - timedelta(hours=1)),
        )
    ]
    db_session.add_all(memories)
    await db_session.commit()
    monkeypatch.setattr(settings, "embedding_backfill_request_size", 2)
    calls: list[list[str]] = []

    async def fake_embed(texts):
        calls.append(texts)
        return [[0.5] * DIM for _ in texts]

    with patch(
        "app.tasks.embedding_backfill.generate_embeddings_batch",
        side_effect=fake_embed,
    ):
        fixed = await backfill_missing_embeddings(db_session)

    assert fixed == 3
    assert calls == [["oldest", "middle"], ["newest"]]


@pytest.mark.anyio
async def test_backfill_uses_configured_database_batch(
    db_session, backfill_resident, monkeypatch
):
    now = datetime.now(UTC)
    memories = [
        Memory(
            resident_id="backfill-res",
            type="event",
            content=content,
            importance=0.5,
            source="agent_action",
            embedding=None,
            created_at=now + timedelta(minutes=minutes),
        )
        for content, minutes in (("first", 0), ("second", 1), ("third", 2))
    ]
    db_session.add_all(memories)
    await db_session.commit()
    monkeypatch.setattr(settings, "embedding_backfill_batch_size", 2)
    calls: list[list[str]] = []

    async def fake_embed(texts):
        calls.append(texts)
        return [[0.5] * DIM for _ in texts]

    with patch(
        "app.tasks.embedding_backfill.generate_embeddings_batch",
        side_effect=fake_embed,
    ):
        fixed = await backfill_missing_embeddings(db_session)

    assert fixed == 2
    assert calls == [["first", "second"]]
    for memory in memories:
        await db_session.refresh(memory)
    assert [memory.embedding is not None for memory in memories] == [True, True, False]


@pytest.mark.anyio
async def test_backfill_skips_archived_and_explicit_world_event_trivia(
    db_session, backfill_resident
):
    eligible = Memory(
        resident_id="backfill-res",
        type="event",
        content="eligible legacy event",
        importance=0.5,
        source="world_event",
        metadata_json=None,
        embedding=None,
    )
    trivia = Memory(
        resident_id="backfill-res",
        type="event",
        content="intentionally vectorless trivia",
        importance=0.5,
        source="world_event",
        metadata_json={"tier": "trivia"},
        embedding=None,
    )
    archived = Memory(
        resident_id="backfill-res",
        type="event",
        content="archived event",
        importance=0.5,
        source="agent_action",
        archived_at=datetime.now(UTC),
        embedding=None,
    )
    db_session.add_all([eligible, trivia, archived])
    await db_session.commit()
    calls: list[list[str]] = []

    async def fake_embed(texts):
        calls.append(texts)
        return [[0.5] * DIM for _ in texts]

    with patch(
        "app.tasks.embedding_backfill.generate_embeddings_batch",
        side_effect=fake_embed,
    ):
        fixed = await backfill_missing_embeddings(db_session)

    assert fixed == 1
    assert calls == [["eligible legacy event"]]
    await db_session.refresh(eligible)
    await db_session.refresh(trivia)
    await db_session.refresh(archived)
    assert eligible.embedding == [0.5] * DIM
    assert trivia.embedding is None
    assert archived.embedding is None


@pytest.mark.anyio
async def test_backfill_reports_progress_metrics(
    db_session, backfill_resident, caplog
):
    await _add_memory(db_session, "backfill-res", "metric row", None)

    async def fake_embed(texts):
        return [[0.5] * DIM for _ in texts]

    with caplog.at_level("INFO"), patch(
        "app.tasks.embedding_backfill.generate_embeddings_batch",
        side_effect=fake_embed,
    ):
        fixed = await backfill_missing_embeddings(db_session)

    assert fixed == 1
    message = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("EMBEDDING_BACKFILL_ROUND")
    )
    assert "selected=1" in message
    assert "fixed=1" in message
    assert "failed=0" in message
    assert "remaining=0" in message
