"""Rate-limiting tests (OPTIMIZATION_PLAN P1-1, limit sub-item).

Covers:
- WS chat_msg sliding window: N allowed then rate_limited, charge never
  called when limited (short-circuit before DB/LLM cost).
- WS window resets after the 60s horizon (time.monotonic mocked).
- REST auth register: 5/min by IP -> 429 on the 6th.
- REST forge: 10/min by IP -> 429 on the 11th.
- Limits are configurable via settings (callable decorators read settings
  lazily, ws_limiter reads settings lazily).

conftest's autouse `_reset_rate_limiters` clears slowapi storage before each
test; the WS limiter now lives in Redis and starts empty because conftest's
`_fake_redis` installs a fresh in-memory server per test. Tests that need a
specific small limit monkeypatch settings.*_per_minute.
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
    await ws_limiter.reset()
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
    limiter = SlidingWindowLimiter(max_per_minute=3, namespace="test-reset")
    assert await limiter.check("k") is True   # 1
    assert await limiter.check("k") is True   # 2
    assert await limiter.check("k") is True   # 3
    assert await limiter.check("k") is False  # 4 -> blocked
    # advance the wall clock well past the 60s window so all stored hits fall
    # outside it and check() admits a new hit
    import app.ws.rate_limiter as rl
    orig_time = rl.time.time
    try:
        rl.time.time = lambda: orig_time() + 120.0
        assert await limiter.check("k") is True
    finally:
        rl.time.time = orig_time


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


@pytest.mark.anyio
async def test_rest_deep_forge_rate_limit(client):
    """deep-start uses the same IP limiter as the other Forge entry points."""
    limit = settings.rest_rate_limit_forge_per_minute
    codes = []
    for i in range(limit + 1):
        response = await client.post(
            "/forge/deep-start",
            json={"character_name": f"deep-{i}", "raw_text": "source"},
        )
        codes.append(response.status_code)
    assert codes[:limit] == [401] * limit
    assert codes[limit] == 429


@pytest.mark.anyio
async def test_rest_guided_answer_rate_limit(client):
    """Guided answers are directly throttled before auth/session lookup."""
    limit = settings.rest_rate_limit_forge_per_minute
    codes = []
    for _ in range(limit + 1):
        response = await client.post(
            "/forge/answer",
            json={"forge_id": "unknown", "answer": "source"},
        )
        codes.append(response.status_code)
    assert codes[:limit] == [401] * limit
    assert codes[limit] == 429


@pytest.mark.anyio
async def test_rest_skill_import_rate_limit(client):
    """Multipart imports are throttled before auth/LLM work."""
    limit = settings.rest_rate_limit_import_per_minute
    codes = []
    for index in range(limit + 1):
        response = await client.post(
            "/residents/import",
            files={"file": ("SKILL.md", b"# Ability\nTest", "text/markdown")},
            data={"name": f"Import {index}", "slug": f"import-{index}"},
        )
        codes.append(response.status_code)
    assert codes[:limit] == [401] * limit
    assert codes[limit] == 429


@pytest.mark.anyio
async def test_rest_resident_edit_rate_limit(client):
    """Persona edits are throttled before auth and optional SBTI work."""
    limit = settings.rest_rate_limit_resident_edit_per_minute
    codes = []
    for index in range(limit + 1):
        response = await client.put(
            "/residents/rate-limit-probe",
            json={"ability_md": f"edit {index}"},
        )
        codes.append(response.status_code)
    assert codes[:limit] == [401] * limit
    assert codes[limit] == 429


# ---------------------------------------------------------------------------
# Configurability
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_ws_limit_configurable_via_settings():
    """Raising settings.ws_rate_limit_per_minute admits more hits."""
    await ws_limiter.reset()
    with patch.object(settings, "ws_rate_limit_per_minute", 100):
        for _ in range(50):
            assert await ws_limiter.check("cfg-user") is True
