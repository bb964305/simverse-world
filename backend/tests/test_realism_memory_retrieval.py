"""Realism P0-2: scored vector retrieval + soft-archive eviction."""
import pytest
from datetime import datetime, timedelta, UTC

from app.config import settings
from app.models.memory import Memory
from app.memory.service import MemoryService


async def _add(db, rid, content, importance, *, emb=None, days_ago=0, type="event"):
    m = Memory(resident_id=rid, type=type, content=content, importance=importance,
               source="agent_action", embedding=emb)
    ts = datetime.now(UTC) - timedelta(days=days_ago)
    m.created_at = ts
    m.last_accessed_at = ts
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


async def test_evict_archives_low_importance_stale_events(db_session):
    m_stale = await _add(db_session, "r1", "闲聊", 0.2, days_ago=120)
    m_fresh = await _add(db_session, "r1", "重要", 0.9, days_ago=1)
    m_reflection = await _add(db_session, "r1", "洞察", 0.1, days_ago=200, type="reflection")
    svc = MemoryService(db_session)
    n = await svc.evict_memories("r1")
    await db_session.refresh(m_stale)
    await db_session.refresh(m_fresh)
    await db_session.refresh(m_reflection)
    assert n == 1
    assert m_stale.archived_at is not None       # 低分 + 90 天未访问的 event 归档
    assert m_fresh.archived_at is None            # 高分保留
    assert m_reflection.archived_at is None       # 非 event 不归档


async def test_evict_is_idempotent(db_session):
    await _add(db_session, "r1b", "闲聊", 0.2, days_ago=120)
    svc = MemoryService(db_session)
    assert await svc.evict_memories("r1b") == 1
    assert await svc.evict_memories("r1b") == 0   # already archived → not re-counted


async def test_scored_retrieval_prefers_semantic_over_high_importance(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_enabled", True)
    # 语义相关的低分记忆 emb 与 query 同向；语义无关的高分记忆 emb 正交
    rel = await _add(db_session, "r2", "关于猫的对话", 0.3, emb=[1.0] + [0.0] * 1023, days_ago=1)
    await _add(db_session, "r2", "无关高分", 0.95, emb=[0.0, 1.0] + [0.0] * 1022, days_ago=1)
    svc = MemoryService(db_session)
    got = await svc._search_events_scored("r2", [1.0] + [0.0] * 1023, limit=2)
    assert got[0].id == rel.id   # 语义相关排前，尽管 importance 更低


async def test_scored_retrieval_excludes_archived(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_enabled", True)
    keep = await _add(db_session, "r3", "留存", 0.3, emb=[1.0] + [0.0] * 1023, days_ago=1)
    gone = await _add(db_session, "r3", "已归档", 0.3, emb=[1.0] + [0.0] * 1023, days_ago=1)
    gone.archived_at = datetime.now(UTC)
    await db_session.commit()
    svc = MemoryService(db_session)
    got = await svc._search_events_scored("r3", [1.0] + [0.0] * 1023, limit=10)
    ids = {m.id for m in got}
    assert keep.id in ids and gone.id not in ids


async def test_retrieve_context_off_uses_static_ranking(db_session, monkeypatch):
    monkeypatch.setattr(settings, "realism_enabled", False)
    await _add(db_session, "r4", "高分", 0.9, emb=[1.0] + [0.0] * 1023, days_ago=1)
    svc = MemoryService(db_session)
    ctx = await svc.retrieve_context("r4", query_text="任意查询")
    assert len(ctx["events"]) == 1   # 关掉时走静态排序，不炸
