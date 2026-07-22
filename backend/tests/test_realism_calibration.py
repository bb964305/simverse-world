"""Realism P1-12: importance quantile calibration + double-condition shift gate."""
import pytest
from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings
from app.models.memory import Memory
from app.memory.service import MemoryService


async def _seed_events(db, rid, n, *, raw):
    now = datetime.now(UTC)
    for i in range(n):
        m = Memory(resident_id=rid, type="event", content=f"e{i}", importance=0.5,
                   source="agent_action", metadata_json={"raw_importance": raw})
        m.created_at = now - timedelta(minutes=i)
        db.add(m)
    await db.commit()


@pytest.mark.anyio
async def test_normalize_returns_raw_when_little_history(db_session):
    svc = MemoryService(db_session)
    await _seed_events(db_session, "r1", 3, raw=0.9)
    assert await svc._normalize_importance("r1", 0.9) == 0.9   # < 10 history → raw


@pytest.mark.anyio
async def test_normalize_deflates_inflated_scores(db_session):
    svc = MemoryService(db_session)
    await _seed_events(db_session, "r2", 12, raw=0.9)   # everything inflated to 0.9
    # a new 0.9 is not exceptional in an all-0.9 distribution → mid-rank ~0.5
    assert await svc._normalize_importance("r2", 0.9) == pytest.approx(0.5)
    # a genuinely higher raw sits at the top
    assert await svc._normalize_importance("r2", 0.95) == pytest.approx(1.0)


@pytest.mark.anyio
async def test_add_memory_stores_normalized_and_raw(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_enabled", True)
    svc = MemoryService(db_session)
    await _seed_events(db_session, "r3", 12, raw=0.9)
    mem = await svc.add_memory("r3", "event", "又一件事", 0.9, "agent_action")
    assert mem.importance == pytest.approx(0.5)                 # normalized percentile
    assert mem.metadata_json["raw_importance"] == 0.9          # raw preserved


@pytest.mark.anyio
async def test_shift_gate_double_condition(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_enabled", True)
    svc = MemoryService(db_session)

    def _res(valence):
        r = MagicMock()
        r.id = "rr"
        r.mood_json = {"valence": valence, "arousal": 0.7, "label": "x"}
        return r

    def _mem(imp):
        m = MagicMock()
        m.importance = imp
        return m

    async def run(resident, memories):
        with patch("app.memory.service.EvolutionService") as Evo:
            inst = AsyncMock()
            Evo.return_value = inst
            with patch.object(svc, "count_events_since_last_reflection", AsyncMock(return_value=0)):
                await svc._run_evolution_hooks(resident, memories)
            return inst.evaluate_shift.await_count

    # P95 percentile AND |valence|>0.5 → shift fires
    assert await run(_res(0.8), [_mem(0.96)]) == 1
    # high percentile but calm → no shift
    assert await run(_res(0.1), [_mem(0.96)]) == 0
    # strong valence but low percentile → no shift
    assert await run(_res(0.8), [_mem(0.5)]) == 0
