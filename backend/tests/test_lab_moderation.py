"""Phase 4 (recovery plan) — content-entry moderation gate (gap #6, content part).

A task title/brief passes a moderation gate BEFORE any hold. The gate is
objective + structural (emptiness, length, control characters) plus a pluggable
operator blocklist (settings.lab_task_blocklist) — the substantive content
policy stays operator-supplied rather than invented here. A rejection returns a
stable CODE (never the raw content) which create_task records content-free in
telemetry and maps to a LabTaskError before charging the issuer.
"""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.lab import moderation
from app.models.user import User
from app.models.resident import Resident
from app.services import coin_service
from app.services import lab_task_service as svc


def test_clean_task_passes():
    assert moderation.moderate_task("调研任务", "请帮我调研一下 X 的资料") is None


def test_structural_rejections():
    assert moderation.moderate_task("", "brief") == "empty_title"
    assert moderation.moderate_task("   ", "brief") == "empty_title"
    assert moderation.moderate_task("x" * 201, "brief") == "title_too_long"
    assert moderation.moderate_task("ok", "y" * 20000) == "brief_too_long"
    assert moderation.moderate_task("ok\x00title", "brief") == "control_chars"


def test_blocklist_is_pluggable(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "lab_task_blocklist", ["forbidden_term"], raising=False)
    assert moderation.moderate_task("a forbidden_term here", "brief") == "blocked_term"
    assert moderation.moderate_task("clean", "also has forbidden_term inside") == "blocked_term"
    assert moderation.moderate_task("clean", "totally fine") is None


@pytest.fixture
def lab_env(db_engine, monkeypatch):
    from app.config import settings
    for k, v in {
        "lab_enabled": True, "lab_adapter": "mock", "lab_platform_fee_rate": 0.1,
        "lab_default_budget_usd": 0.5, "lab_sc_per_usd": 100, "lab_daily_tasks_per_user": 20,
        "lab_task_blocklist": ["contraband"],
    }.items():
        monkeypatch.setattr(settings, k, v, raising=False)
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch("app.lab.runner.async_session", factory), \
         patch("app.services.lab_task_service.async_session", factory), \
         patch("app.services.lab_task_service.emit", new_callable=AsyncMock):
        yield factory


@pytest.mark.anyio
async def test_moderated_task_rejected_before_hold(lab_env):
    factory = lab_env
    async with factory() as s:
        s.add(User(id="issuer", name="I", email="i@t.com", soul_coin_balance=1000))
        s.add(Resident(slug="sage", name="Sage", creator_id="system", resident_type="npc",
                       meta_json={"lab": {"access": True}}))
        await s.commit()

    async with factory() as s:
        with pytest.raises(svc.LabTaskError):
            await svc.create_task(
                s, issuer_id="issuer", title="selling contraband", brief="...",
                scopes=["web_search"], reward_sc=100, researcher_slug="sage",
            )
    async with factory() as s:
        assert await coin_service.get_balance(s, "issuer") == 1000  # never charged
