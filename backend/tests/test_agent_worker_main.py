"""P0-3a: standalone agent-worker entrypoint (python -m app.agent.main).

main() runs agent_loop + heat_cron_loop + embedding_backfill_loop
concurrently and shuts down cleanly on SIGTERM/SIGINT or task cancel.
"""
import asyncio
import os
import signal

import pytest
from unittest.mock import MagicMock, patch

from app.agent import main as agent_main


async def _forever():
    await asyncio.Event().wait()


def _loop_mocks():
    """Loop stand-ins that run until cancelled and record being started."""
    heat_mock = MagicMock(side_effect=_forever)
    backfill_mock = MagicMock(side_effect=_forever)
    run_mock = MagicMock(side_effect=_forever)
    return heat_mock, backfill_mock, run_mock


@pytest.mark.anyio
async def test_main_starts_all_loops_and_cancels_cleanly():
    heat_mock, backfill_mock, run_mock = _loop_mocks()
    with patch("app.agent.main.heat_cron_loop", heat_mock), \
         patch("app.agent.main.embedding_backfill_loop", backfill_mock), \
         patch("app.agent.main.agent_loop") as mock_agent_loop:
        mock_agent_loop.run = run_mock
        worker = asyncio.create_task(agent_main.main())
        await asyncio.sleep(0.05)

        heat_mock.assert_called_once()
        backfill_mock.assert_called_once()
        run_mock.assert_called_once()

        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


@pytest.mark.anyio
async def test_main_exits_gracefully_on_sigterm():
    heat_mock, backfill_mock, run_mock = _loop_mocks()
    with patch("app.agent.main.heat_cron_loop", heat_mock), \
         patch("app.agent.main.embedding_backfill_loop", backfill_mock), \
         patch("app.agent.main.agent_loop") as mock_agent_loop:
        mock_agent_loop.run = run_mock
        worker = asyncio.create_task(agent_main.main())
        await asyncio.sleep(0.05)

        os.kill(os.getpid(), signal.SIGTERM)
        # Graceful exit: main() returns without raising
        await asyncio.wait_for(worker, timeout=5)
