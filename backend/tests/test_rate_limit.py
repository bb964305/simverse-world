"""Rate-limiting tests (OPTIMIZATION_PLAN P1-1, limit sub-item).

Covers:
- WS chat_msg sliding window: N allowed then rate_limited, charge never
  called when limited (short-circuit before DB/LLM cost).
- WS window resets after the 60s horizon (time.monotonic mocked).
- REST auth register: 5/min by IP -> 429 on the 6th.
- REST forge: 10/min by IP -> 429 on the 11th.
- Limits are configurable via settings (callable decorators read settings
  lazily, ws_limiter reads settings lazily).

conftest's autouse `_reset_rate_limiters` clears slowapi storage + ws_limiter
before each test, so each test starts from an empty window. Tests that need
a specific small limit monkeypatch settings.*_per_minute.
"""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.ws.handlers import chat as chat_handler
from app.ws.handlers.context import ConnectionContext
from app.ws.rate_limiter import SlidingWindowLimiter, ws_limiter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeManager:
    """Captures manager.send payloads for assertion."""

    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, user_id: str, data: dict):
        self.sent.append(data)


def _make_ctx(user_id: str = "user-1") -> ConnectionContext:
    """A context that reports in_chat=True with a minimal resident stub."""
    resident = type("R", (), {
        "id": "r-1",
        "slug": "resident",
        "token_cost_per_turn": 1,
        "status": "chatting",
    })()
    return ConnectionContext(
        user_id=user_id, user_name=user_id,
        conversation_id="c-1", resident=resident,
    )


# ---------------------------------------------------------------------------
# WS sliding window
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_ws_chat_rate_limit_short_circuits_before_charge():
    """Beyond the limit, handle_chat_msg emits rate_limited and never reaches
    charge() — proving the DB/LLM cost path is skipped."""
    ws_limiter.reset()
    fake = FakeManager()
    with patch.object(chat_handler, "manager", fake), \
         patch.object(chat_handler, "charge", new=AsyncMock(side_effect=AssertionError("charge must NOT run when rate-limited"))):
        ctx = _make_ctx()
        data = {"type": "chat_msg", "text": "hi"}
        types = []
        for _ in range(settings.ws_rate_limit_per_minute + 3):
            fake.sent.clear()
            await chat_handler.handle_chat_msg(ctx, data)
            types.append(fake.sent[0].get("type"))
        allowed = [t for t in types if t != "rate_limited"]
        blocked = [t for t in types if t == "rate_limited"]
        assert len(allowed) == settings.ws_rate_limit_per_minute
        assert len(blocked) == 3
        # the rate_limited message carries the configured limit
        fake.sent.clear()
        await chat_handler.handle_chat_msg(ctx, data)
        msg = fake.sent[0]
    assert msg["type"] == "rate_limited"
    assert msg["limit_per_minute"] == settings.ws_rate_limit_per_minute


@pytest.mark.anyio
async def test_ws_rate_limit_resets_after_window():
    """After the 60s horizon elapses, the window is empty again."""
    limiter = SlidingWindowLimiter(max_per_minute=3)
    assert limiter.check("k") is True   # 1
    assert limiter.check("k") is True   # 2
    assert limiter.check("k") is True   # 3
    assert limiter.check("k") is False  # 4 -> blocked
    # advance the monotonic clock well past the 60s window so all stored
    # hits expire and check() admits a new hit
    import app.ws.rate_limiter as rl
    orig_monotonic = rl.time.monotonic
    try:
        rl.time.monotonic = lambda: orig_monotonic() + 120.0
        assert limiter.check("k") is True
    finally:
        rl.time.monotonic = orig_monotonic


# ---------------------------------------------------------------------------
# REST slowapi
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_rest_register_rate_limit(client):
    """auth/register is capped at rest_rate_limit_register_per_minute/min."""
    limit = settings.rest_rate_limit_register_per_minute
    codes = []
    for i in range(limit + 2):
        r = await client.post("/auth/register", json={
            "name": f"u{i}", "email": f"u{i}@test.com", "password": "12345678",
        })
        codes.append(r.status_code)
    # first `limit` succeed (200), the rest are 429
    assert codes[:limit] == [200] * limit
    assert codes[limit:] == [429, 429]


@pytest.mark.anyio
async def test_rest_forge_rate_limit(client):
    """forge/quick is capped at rest_rate_limit_forge_per_minute/min (no auth
    -> requests are rejected early but still counted by the limiter)."""
    limit = settings.rest_rate_limit_forge_per_minute
    codes = []
    for i in range(limit + 1):
        r = await client.post("/forge/quick", json={
            "name": f"q{i}", "raw_text": "x",
        })
        codes.append(r.status_code)
    # all unauthenticated (401) until the limit bites, then 429
    assert codes[:limit] == [401] * limit
    assert codes[limit] == 429


# ---------------------------------------------------------------------------
# Configurability
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_ws_limit_configurable_via_settings():
    """Raising settings.ws_rate_limit_per_minute admits more hits."""
    ws_limiter.reset()
    with patch.object(settings, "ws_rate_limit_per_minute", 100):
        for _ in range(50):
            assert ws_limiter.check("cfg-user") is True
