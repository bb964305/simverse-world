"""P0-3a: lifespan only starts background loops when run_background_tasks is on.

When RUN_BACKGROUND_TASKS=false the loops are owned by the standalone
agent-worker process (python -m app.agent.main) instead of the API.
"""
import pytest
from unittest.mock import AsyncMock, patch

from app.config import settings
from app.main import app, lifespan


@pytest.mark.anyio
async def test_lifespan_starts_background_tasks_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "auto_create_tables", False)
    monkeypatch.setattr(settings, "run_background_tasks", True)
    monkeypatch.setattr(settings, "resident_sprite_enabled", True)

    with patch("app.main.heat_cron_loop", new=AsyncMock()) as heat_mock, \
         patch("app.main.embedding_backfill_loop", new=AsyncMock()) as backfill_mock, \
         patch("app.main.resident_sprite_worker_loop", new=AsyncMock()) as sprite_mock, \
         patch("app.main.agent_loop") as mock_agent_loop:
        mock_agent_loop.run = AsyncMock()
        async with lifespan(app):
            pass
        heat_mock.assert_called_once()
        backfill_mock.assert_called_once()
        sprite_mock.assert_called_once()
        mock_agent_loop.run.assert_called_once()


@pytest.mark.anyio
async def test_lifespan_skips_background_tasks_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "auto_create_tables", False)
    monkeypatch.setattr(settings, "run_background_tasks", False)

    with patch("app.main.heat_cron_loop", new=AsyncMock()) as heat_mock, \
         patch("app.main.embedding_backfill_loop", new=AsyncMock()) as backfill_mock, \
         patch("app.main.resident_sprite_worker_loop", new=AsyncMock()) as sprite_mock, \
         patch("app.main.agent_loop") as mock_agent_loop:
        mock_agent_loop.run = AsyncMock()
        async with lifespan(app):
            pass
        heat_mock.assert_not_called()
        backfill_mock.assert_not_called()
        sprite_mock.assert_not_called()
        mock_agent_loop.run.assert_not_called()


@pytest.mark.anyio
async def test_lifespan_skips_sprite_worker_when_feature_is_disabled(monkeypatch):
    monkeypatch.setattr(settings, "auto_create_tables", False)
    monkeypatch.setattr(settings, "run_background_tasks", True)
    monkeypatch.setattr(settings, "resident_sprite_enabled", False)

    with patch("app.main.resident_sprite_worker_loop", new=AsyncMock()) as sprite_mock:
        async with lifespan(app):
            pass
        sprite_mock.assert_not_called()
