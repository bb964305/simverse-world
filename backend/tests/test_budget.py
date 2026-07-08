"""P1-1 budget circuit breaker: tier computation, per-user cap, forge gate."""
import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime, UTC, timedelta
from sqlalchemy.pool import StaticPool
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import settings
from app.database import Base
from app.models.llm_usage import LLMUsage
from app.llm import budget
from app.llm.budget import (
    BudgetTier, tier_for_fraction, background_tier,
    global_spend_today, user_spend_today, user_over_budget, forge_blocked,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def bsession():
    """Session on a shared in-memory sqlite with metering enabled."""
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings.llm_metering_enabled = True
    async with factory() as s:
        yield s
    settings.llm_metering_enabled = False
    await engine.dispose()


async def _spend(session, cost, *, owner="system", user_id=None, ago_days=0):
    session.add(LLMUsage(
        scenario="decide", model="m", owner=owner, cost_usd=cost,
        user_id=user_id, source="usage",
        ts=datetime.now(UTC) - timedelta(days=ago_days),
    ))
    await session.commit()


# ---------- tier boundaries ----------

def test_tier_for_fraction_boundaries():
    assert tier_for_fraction(0.0) == BudgetTier.NORMAL
    assert tier_for_fraction(0.79) == BudgetTier.NORMAL
    assert tier_for_fraction(0.80) == BudgetTier.THROTTLE
    assert tier_for_fraction(0.94) == BudgetTier.THROTTLE
    assert tier_for_fraction(0.95) == BudgetTier.RULE_ONLY
    assert tier_for_fraction(0.999) == BudgetTier.RULE_ONLY
    assert tier_for_fraction(1.0) == BudgetTier.PLAYER_ONLY
    assert tier_for_fraction(3.0) == BudgetTier.PLAYER_ONLY


# ---------- spend queries ----------

async def test_global_spend_sums_today_only(bsession):
    await _spend(bsession, 0.10)
    await _spend(bsession, 0.05)
    await _spend(bsession, 99.0, ago_days=2)  # old, excluded
    assert abs(await global_spend_today(bsession) - 0.15) < 1e-9


async def test_user_spend_filters_by_user(bsession):
    await _spend(bsession, 0.2, owner="user", user_id="u1")
    await _spend(bsession, 0.3, owner="user", user_id="u2")
    assert abs(await user_spend_today(bsession, "u1") - 0.2) < 1e-9


# ---------- background_tier ----------

async def test_background_tier_scales_with_spend(bsession, monkeypatch):
    monkeypatch.setattr(settings, "budget_global_daily_usd", 1.0)
    assert await background_tier(bsession) == BudgetTier.NORMAL
    await _spend(bsession, 0.80)
    assert await background_tier(bsession) == BudgetTier.THROTTLE
    await _spend(bsession, 0.15)   # 0.95
    assert await background_tier(bsession) == BudgetTier.RULE_ONLY
    await _spend(bsession, 0.06)   # 1.01
    assert await background_tier(bsession) == BudgetTier.PLAYER_ONLY


async def test_background_tier_disabled_metering_is_normal(bsession, monkeypatch):
    monkeypatch.setattr(settings, "budget_global_daily_usd", 0.01)
    await _spend(bsession, 5.0)
    settings.llm_metering_enabled = False
    assert await background_tier(bsession) == BudgetTier.NORMAL


async def test_background_tier_zero_budget_disables(bsession, monkeypatch):
    monkeypatch.setattr(settings, "budget_global_daily_usd", 0.0)
    await _spend(bsession, 5.0)
    assert await background_tier(bsession) == BudgetTier.NORMAL


# ---------- per-user cap ----------

async def test_user_over_budget(bsession, monkeypatch):
    monkeypatch.setattr(settings, "budget_user_daily_usd", 0.5)
    assert await user_over_budget(bsession, "u1") is False
    await _spend(bsession, 0.5, owner="user", user_id="u1")
    assert await user_over_budget(bsession, "u1") is True
    # a different user is unaffected
    assert await user_over_budget(bsession, "u2") is False


async def test_user_over_budget_none_user(bsession):
    assert await user_over_budget(bsession, None) is False


# ---------- forge gate ----------

async def test_forge_blocked_when_global_exhausted(bsession, monkeypatch):
    monkeypatch.setattr(settings, "budget_global_daily_usd", 1.0)
    monkeypatch.setattr(settings, "budget_user_daily_usd", 100.0)
    assert await forge_blocked(bsession, "u1") is False
    await _spend(bsession, 1.0)  # global 100%
    assert await forge_blocked(bsession, "u1") is True


async def test_forge_blocked_when_user_over(bsession, monkeypatch):
    monkeypatch.setattr(settings, "budget_global_daily_usd", 100.0)
    monkeypatch.setattr(settings, "budget_user_daily_usd", 0.3)
    await _spend(bsession, 0.3, owner="user", user_id="u1")
    assert await forge_blocked(bsession, "u1") is True
    assert await forge_blocked(bsession, "u2") is False


# ---------- player-chat handler gate ----------

class _FakeManager:
    def __init__(self):
        self.sent = []

    async def send(self, user_id, data):
        self.sent.append(data)


async def test_player_chat_blocked_when_user_over_budget(monkeypatch):
    """handle_chat_msg replies budget_exceeded and never charges once the user
    has spent their daily allowance."""
    from app.ws.handlers import chat as chat_handler
    from app.ws.handlers.context import ConnectionContext
    from app.models.conversation import Conversation

    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        s.add(Conversation(id="c-1", user_id="u1", resident_id="r-1"))
        s.add(LLMUsage(scenario="player_chat", model="m", owner="user",
                       user_id="u1", cost_usd=0.6, source="usage", ts=datetime.now(UTC)))
        await s.commit()

    settings.llm_metering_enabled = True
    monkeypatch.setattr(settings, "budget_user_daily_usd", 0.5)
    resident = type("R", (), {"id": "r-1", "slug": "r", "token_cost_per_turn": 1, "status": "chatting"})()
    ctx = ConnectionContext(user_id="u1", user_name="u1", conversation_id="c-1", resident=resident)
    fake = _FakeManager()

    try:
        with patch.object(chat_handler, "async_session", factory), \
             patch.object(chat_handler, "manager", fake), \
             patch.object(chat_handler, "charge",
                          new=AsyncMock(side_effect=AssertionError("must not charge over budget"))):
            await chat_handler.ws_limiter.reset()
            await chat_handler.handle_chat_msg(ctx, {"type": "chat_msg", "text": "hi"})
    finally:
        settings.llm_metering_enabled = False
        await engine.dispose()

    assert fake.sent and fake.sent[-1]["type"] == "budget_exceeded"
