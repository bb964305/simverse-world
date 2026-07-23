"""Realism P0-3: mood write-back from resident chat + flashbulb importance."""
import pytest
from unittest.mock import AsyncMock, patch

from app.config import settings


@pytest.mark.anyio
async def test_positive_mood_nudges_both(monkeypatch):
    from app.agent.chat import _apply_chat_mood
    monkeypatch.setattr(settings, "realism_enabled", True)
    calls = []

    async def fake(db, res, dv, da=0.0):
        calls.append((res, dv, da))

    with patch("app.agent.chat.apply_mood_event", fake):
        a, b = object(), object()
        await _apply_chat_mood("db", a, b, "positive")
    assert calls == [(a, 0.15, 0.05), (b, 0.15, 0.05)]


@pytest.mark.anyio
async def test_negative_mood(monkeypatch):
    from app.agent.chat import _apply_chat_mood
    monkeypatch.setattr(settings, "realism_enabled", True)
    calls = []

    async def fake(db, res, dv, da=0.0):
        calls.append((dv, da))

    with patch("app.agent.chat.apply_mood_event", fake):
        await _apply_chat_mood("db", object(), object(), "negative")
    assert calls == [(-0.2, 0.1), (-0.2, 0.1)]


@pytest.mark.anyio
async def test_neutral_and_off_are_noops(monkeypatch):
    from app.agent.chat import _apply_chat_mood
    calls = []

    async def fake(*a, **k):
        calls.append(1)

    with patch("app.agent.chat.apply_mood_event", fake):
        monkeypatch.setattr(settings, "realism_enabled", True)
        await _apply_chat_mood("db", object(), object(), "neutral")
        monkeypatch.setattr(settings, "realism_enabled", False)
        await _apply_chat_mood("db", object(), object(), "positive")
    assert calls == []


def test_flashbulb_boosts_importance_high_arousal_negative():
    from app.agent.phases.memorize.basic import _flashbulb_boost
    hi = _flashbulb_boost({"valence": -0.8, "arousal": 0.9})
    calm = _flashbulb_boost({"valence": 0.0, "arousal": 0.5})
    assert hi > calm
    assert hi == pytest.approx(0.2 * 0.8 * 0.9)
    assert _flashbulb_boost(None) == 0.0
