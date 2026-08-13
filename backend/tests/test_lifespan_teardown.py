"""S13 加固: reaper/subscriber 不吞 cancel + lifespan teardown 有界等待.

三个长驻任务 (run_subscriber / run_agent_presence_reaper /
run_agent_npc_chat_reaper) 收到 cancel 必须让任务终态为 cancelled
(re-raise, 不 break 吞掉), 使 teardown 的等待能如实收敛。

teardown 本身: cancel 后必须真正等任务收尾 (清理逻辑跑完), 但等待有界
(_SHUTDOWN_TIMEOUT) —— 单个任务吞 cancel 也不能卡死进程退出。
"""
import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.main import app, lifespan
import app.main as main_module
import app.services.player_npc_chat_service as pncs
from app.ws.manager import manager


async def _forever():
    await asyncio.sleep(3600)


# --------------------------------------------------------------------- #
# 三个长驻任务: cancel 后终态必须是 cancelled (不吞 CancelledError)      #
# --------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_run_subscriber_ends_cancelled_on_cancel():
    task = asyncio.create_task(manager.run_subscriber())
    # 让它完成 SUBSCRIBE 并进入 listen() (cancel 落在 try 块内)
    await asyncio.sleep(0.1)
    task.cancel()
    await asyncio.wait([task], timeout=2)
    assert task.cancelled(), "run_subscriber 吞掉了 CancelledError"


@pytest.mark.anyio
async def test_agent_presence_reaper_ends_cancelled_on_cancel(monkeypatch):
    started = asyncio.Event()

    async def _block():
        started.set()
        await asyncio.Event().wait()

    # 把 cancel 定点打进 try 块内 (expire_agent_presences 执行中)
    monkeypatch.setattr(manager, "expire_agent_presences", _block)
    task = asyncio.create_task(manager.run_agent_presence_reaper())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.wait([task], timeout=2)
    assert task.cancelled(), "run_agent_presence_reaper 吞掉了 CancelledError"


@pytest.mark.anyio
async def test_npc_chat_reaper_ends_cancelled_on_cancel(monkeypatch):
    started = asyncio.Event()

    async def _block(db):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(pncs, "recover_expired_npc_chat_turns", _block)
    task = asyncio.create_task(pncs.run_agent_npc_chat_reaper())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    await asyncio.wait([task], timeout=2)
    assert task.cancelled(), "run_agent_npc_chat_reaper 吞掉了 CancelledError"


# --------------------------------------------------------------------- #
# lifespan teardown: 真正等收尾 + 有界                                   #
# --------------------------------------------------------------------- #


def _settings(monkeypatch):
    monkeypatch.setattr(settings, "auto_create_tables", False)
    monkeypatch.setattr(settings, "run_background_tasks", True)
    monkeypatch.setattr(settings, "resident_sprite_enabled", False)


def _patches(heat_loop):
    fake_agent_loop = MagicMock()
    fake_agent_loop.run = _forever
    return [
        patch("app.main.heat_cron_loop", heat_loop),
        patch("app.main.event_cron_loop", _forever),
        patch("app.main.nightly_cron_loop", _forever),
        patch("app.main.embedding_backfill_loop", _forever),
        patch("app.main.caravan_lifecycle_loop", _forever),
        patch("app.main.agent_loop", fake_agent_loop),
    ]


@pytest.mark.anyio
async def test_lifespan_teardown_awaits_task_cleanup(monkeypatch):
    """cancel 后 teardown 必须等到任务清理逻辑真正跑完再返回."""
    _settings(monkeypatch)
    cleaned = asyncio.Event()

    async def _slow_cleanup_loop():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # 模拟需要真实 await 的清理
            cleaned.set()
            raise

    patches = _patches(_slow_cleanup_loop)
    for p in patches:
        p.start()
    try:
        async with lifespan(app):
            await asyncio.sleep(0.05)  # 让各 loop 真正跑起来
    finally:
        for p in patches:
            p.stop()

    assert cleaned.is_set(), "teardown 没有等任务收尾就返回了"


@pytest.mark.anyio
async def test_lifespan_teardown_bounded_with_stuck_task(monkeypatch):
    """单个任务吞 cancel 卡死时, teardown 在 _SHUTDOWN_TIMEOUT 内有界完成."""
    _settings(monkeypatch)
    monkeypatch.setattr(main_module, "_SHUTDOWN_TIMEOUT", 0.5)

    stuck: list[asyncio.Task] = []
    cancels = {"n": 0}

    async def _swallow_first_cancel():
        stuck.append(asyncio.current_task())
        while True:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancels["n"] += 1
                if cancels["n"] >= 2:  # 第二次 cancel 才放行, 测试收尾用
                    raise

    patches = _patches(_swallow_first_cancel)
    for p in patches:
        p.start()
    start = time.monotonic()
    try:
        async with lifespan(app):
            await asyncio.sleep(0.05)
    finally:
        for p in patches:
            p.stop()
    elapsed = time.monotonic() - start

    assert elapsed < 5.0, f"teardown 未有界完成: {elapsed:.1f}s"
    assert cancels["n"] >= 1, "卡死任务根本没收到 cancel"
    # 收尾: 第二次 cancel 放行, 别把僵尸任务留给事件循环关闭阶段
    assert stuck
    stuck[0].cancel()
    await asyncio.wait(stuck, timeout=2)
