import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from app.models.resident import Resident
from app.agent.loop import AgentLoop
from app.agent.actions import ActionType, ActionResult
from app.agent.scheduler import DailySchedule
from app.llm.budget import BudgetTier


@pytest.fixture
def loop_session_factory(db_engine):
    """Session factory bound to the test engine, to patch app.agent.loop.async_session."""
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def loop_residents(db_session):
    residents = []
    for i in range(3):
        r = Resident(
            id=f"loop-res-{i}",
            slug=f"loop-res-{i}",
            name=f"LoopRes{i}",
            district="engineering",
            status="idle",
            ability_md="Loops",
            persona_md="Patient",
            soul_md="Persistent",
            creator_id="c1",
            meta_json={"sbti": {"type": "GOGO", "type_name": "行者", "dimensions": {
                "S1": "H", "S2": "H", "S3": "M",
                "E1": "H", "E2": "M", "E3": "H",
                "A1": "M", "A2": "M", "A3": "H",
                "Ac1": "H", "Ac2": "H", "Ac3": "H",
                "So1": "M", "So2": "H", "So3": "M",
            }}},
        )
        db_session.add(r)
        residents.append(r)
    await db_session.commit()
    return residents


@pytest.mark.anyio
async def test_agent_loop_tick_round_runs(loop_session_factory, loop_residents):
    """_tick_round should call resident_tick for each active resident."""
    loop = AgentLoop()

    tick_results = [
        ActionResult(action=ActionType.IDLE, target_slug=None, target_tile=None, reason="rest"),
        ActionResult(action=ActionType.WANDER, target_slug=None, target_tile=(80, 55), reason="restless"),
        None,  # third resident skipped
    ]
    call_idx = 0

    async def mock_tick(db, resident):
        nonlocal call_idx
        result = tick_results[min(call_idx, len(tick_results) - 1)]
        call_idx += 1
        return result

    with patch("app.agent.loop.async_session", loop_session_factory):
        with patch("app.agent.loop.resident_tick", side_effect=mock_tick):
            with patch("app.agent.loop.should_tick", return_value=True), \
         patch("app.agent.loop.get_activity_probability", return_value=0.5):
                with patch("app.agent.loop.build_schedule", return_value=MagicMock(
                    wake_hour=6, sleep_hour=23, peak_hours=[10], social_slots=[14], rest_ratio=0.3
                )):
                    with patch("app.agent.loop.manager") as mock_manager:
                        mock_manager.broadcast = AsyncMock()
                        await loop._tick_round()

    # All 3 residents should have been evaluated
    assert call_idx == 3


@pytest.mark.anyio
async def test_agent_loop_only_ticks_autonomous_residents(
    db_session, loop_session_factory, loop_residents
):
    player = Resident(
        id="registered-player",
        slug="registered-player",
        name="Registered Player",
        resident_type="player",
        district="engineering",
        status="idle",
        creator_id="player-user",
    )
    db_session.add(player)
    await db_session.commit()
    ticked: list[str] = []

    async def capture_tick(db, resident):
        ticked.append(resident.slug)
        return None

    with patch("app.agent.loop.async_session", loop_session_factory), \
         patch("app.agent.loop.resident_tick", side_effect=capture_tick), \
         patch("app.agent.loop.should_tick", return_value=True), \
         patch("app.agent.loop.get_activity_probability", return_value=0.5), \
         patch("app.agent.loop.build_schedule", return_value=MagicMock(
             wake_hour=6, sleep_hour=23, peak_hours=[10],
             social_slots=[14], rest_ratio=0.3)):
        await AgentLoop()._tick_round()

    assert set(ticked) == {r.slug for r in loop_residents}
    assert player.slug not in ticked


@pytest.mark.anyio
async def test_agent_loop_each_tick_gets_own_session(db_session, loop_session_factory, loop_residents):
    """P0-1 regression: every tick must run in its own AsyncSession, never a shared one."""
    loop = AgentLoop()
    seen_sessions: list[AsyncSession] = []

    async def capture_tick(db, resident):
        seen_sessions.append(db)
        return None

    with patch("app.agent.loop.async_session", loop_session_factory):
        with patch("app.agent.loop.resident_tick", side_effect=capture_tick):
            with patch("app.agent.loop.should_tick", return_value=True), \
         patch("app.agent.loop.get_activity_probability", return_value=0.5):
                with patch("app.agent.loop.build_schedule", return_value=MagicMock(
                    wake_hour=6, sleep_hour=23, peak_hours=[10], social_slots=[14], rest_ratio=0.3
                )):
                    await loop._tick_round()

    assert len(seen_sessions) == 3
    # All sessions distinct from each other and from any outer session
    assert len({id(s) for s in seen_sessions}) == 3
    assert all(s is not db_session for s in seen_sessions)


@pytest.mark.anyio
async def test_agent_loop_respects_max_concurrent(loop_session_factory, loop_residents):
    """AgentLoop should use a semaphore limiting concurrent ticks."""
    loop = AgentLoop()
    concurrent_count = 0
    max_seen = 0

    async def slow_tick(db, resident):
        nonlocal concurrent_count, max_seen
        concurrent_count += 1
        max_seen = max(max_seen, concurrent_count)
        import asyncio
        await asyncio.sleep(0.01)
        concurrent_count -= 1
        return None

    with patch("app.agent.loop.async_session", loop_session_factory):
        with patch("app.agent.loop.resident_tick", side_effect=slow_tick):
            with patch("app.agent.loop.should_tick", return_value=True), \
         patch("app.agent.loop.get_activity_probability", return_value=0.5):
                with patch("app.agent.loop.build_schedule", return_value=MagicMock(
                    wake_hour=6, sleep_hour=23, peak_hours=[10], social_slots=[14], rest_ratio=0.3
                )):
                    with patch("app.agent.loop.settings") as mock_settings:
                        mock_settings.agent_max_concurrent = 2
                        mock_settings.agent_enabled = True
                        mock_settings.agent_max_daily_actions = 20
                        await loop._tick_round()

    # max concurrent should not exceed limit
    assert max_seen <= 2


@pytest.mark.anyio
async def test_agent_loop_broadcasts_movement(loop_session_factory, loop_residents):
    """Loop should broadcast resident_move for WANDER actions."""
    loop = AgentLoop()
    broadcasts: list[dict] = []

    async def mock_broadcast(data, exclude=None):
        broadcasts.append(data)

    wander_result = ActionResult(
        action=ActionType.WANDER, target_slug=None, target_tile=(80, 55), reason="restless"
    )

    with patch("app.agent.loop.async_session", loop_session_factory):
        with patch("app.agent.loop.resident_tick", return_value=wander_result):
            with patch("app.agent.loop.should_tick", return_value=True), \
         patch("app.agent.loop.get_activity_probability", return_value=0.5):
                with patch("app.agent.loop.build_schedule", return_value=MagicMock(
                    wake_hour=6, sleep_hour=23, peak_hours=[10], social_slots=[14], rest_ratio=0.3
                )):
                    with patch("app.agent.loop.manager") as mock_manager:
                        mock_manager.broadcast = AsyncMock(side_effect=mock_broadcast)
                        await loop._tick_round()

    move_broadcasts = [b for b in broadcasts if b.get("type") == "resident_move"]
    assert len(move_broadcasts) >= 1


@pytest.mark.anyio
async def test_movement_broadcasts_only_committed_market_purchase_frame(db_session):
    """The loop relays the production service's committed frame verbatim."""
    resident = Resident(
        id="market-buyer-id", slug="market-buyer", name="赶集居民",
        creator_id="system", resident_type="npc", district="market_hall",
        status="idle", tile_x=114, tile_y=93,
    )
    db_session.add(resident)
    await db_session.commit()
    movement = ActionResult(
        action=ActionType.VISIT_DISTRICT,
        target_slug="market_hall",
        target_tile=(114, 93),
        reason="去集市购买商队货物",
    )
    frame = {
        "type": "market_purchase",
        "visit_id": "visit-1",
        "resident_slug": resident.slug,
        "purchase_id": "receipt-1",
        "sequence": 1,
        "item_name": "茶叶",
        "quantity": 1,
        "amount_sc": 6,
    }
    broadcasts: list[dict] = []

    with patch(
        "app.services.caravan_market_service.maybe_purchase_for_resident",
        AsyncMock(return_value=frame),
    ) as purchase, patch("app.agent.loop.manager") as mock_manager:
        mock_manager.broadcast = AsyncMock(
            side_effect=lambda data, **_kwargs: broadcasts.append(data)
        )
        await AgentLoop()._handle_action(db_session, resident, movement)

    purchase.assert_awaited_once_with(db_session, resident)
    assert [message["type"] for message in broadcasts] == [
        "resident_move", "market_purchase",
    ]
    assert broadcasts[-1] is frame


@pytest.mark.anyio
async def test_movement_does_not_invent_purchase_when_service_returns_none(db_session):
    resident = Resident(
        id="market-browser-id", slug="market-browser", name="逛集居民",
        creator_id="system", resident_type="npc", district="market_hall",
        status="walking", tile_x=110, tile_y=94,
    )
    db_session.add(resident)
    await db_session.commit()
    movement = ActionResult(
        action=ActionType.VISIT_DISTRICT,
        target_slug="market_hall",
        target_tile=(114, 93),
        reason="去集市",
    )
    broadcasts: list[dict] = []

    with patch(
        "app.services.caravan_market_service.maybe_purchase_for_resident",
        AsyncMock(return_value=None),
    ), patch("app.agent.loop.manager") as mock_manager:
        mock_manager.broadcast = AsyncMock(
            side_effect=lambda data, **_kwargs: broadcasts.append(data)
        )
        await AgentLoop()._handle_action(db_session, resident, movement)

    assert [message["type"] for message in broadcasts] == ["resident_move"]


@pytest.mark.anyio
async def test_agent_loop_one_failed_tick_doesnt_crash(loop_session_factory, loop_residents):
    """A failing tick should be caught and loop should continue."""
    loop = AgentLoop()
    call_count = 0

    async def flaky_tick(db, resident):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("Simulated tick failure")
        return None

    with patch("app.agent.loop.async_session", loop_session_factory):
        with patch("app.agent.loop.resident_tick", side_effect=flaky_tick):
            with patch("app.agent.loop.should_tick", return_value=True), \
         patch("app.agent.loop.get_activity_probability", return_value=0.5):
                with patch("app.agent.loop.build_schedule", return_value=MagicMock(
                    wake_hour=6, sleep_hour=23, peak_hours=[10], social_slots=[14], rest_ratio=0.3
                )):
                    with patch("app.agent.loop.manager") as mock_manager:
                        mock_manager.broadcast = AsyncMock()
                        # Should not raise
                        await loop._tick_round()

    # All 3 residents should have been attempted
    assert call_count == 3


# ── Budget circuit breaker integration (P1-1, E-24) ──────────────────

@pytest.mark.anyio
async def test_loop_player_only_skips_round(loop_session_factory, loop_residents):
    """At PLAYER_ONLY (>=100%) the whole background round is paused."""
    loop = AgentLoop()
    ticked = 0

    async def spy_tick(db, resident, **kw):
        nonlocal ticked
        ticked += 1
        return None

    with patch("app.agent.loop.async_session", loop_session_factory), \
         patch("app.agent.loop.background_tier", AsyncMock(return_value=BudgetTier.PLAYER_ONLY)), \
         patch("app.agent.loop.resident_tick", side_effect=spy_tick), \
         patch("app.agent.loop.should_tick", return_value=True), \
         patch("app.agent.loop.get_activity_probability", return_value=0.5):
        tier = await loop._tick_round()

    assert tier == BudgetTier.PLAYER_ONLY
    assert ticked == 0  # no resident ticked


@pytest.mark.anyio
async def test_loop_rule_only_forces_plan_and_suppresses_chat(loop_session_factory, loop_residents):
    """At RULE_ONLY (>=95%) ticks run force_plan_only and chat is not initiated."""
    loop = AgentLoop()
    seen_force = []

    async def chat_tick(db, resident, *, force_plan_only=False):
        seen_force.append(force_plan_only)
        return ActionResult(action=ActionType.CHAT_RESIDENT, target_slug="loop-res-0",
                            target_tile=None, reason="plan says chat")

    broadcasts = []

    with patch("app.agent.loop.async_session", loop_session_factory), \
         patch("app.agent.loop.background_tier", AsyncMock(return_value=BudgetTier.RULE_ONLY)), \
         patch("app.agent.loop.resident_tick", side_effect=chat_tick), \
         patch("app.agent.loop.should_tick", return_value=True), \
         patch("app.agent.loop.get_activity_probability", return_value=0.5), \
         patch("app.agent.loop.build_schedule", return_value=MagicMock(
             wake_hour=6, sleep_hour=23, peak_hours=[10], social_slots=[14], rest_ratio=0.3)), \
         patch.object(loop, "_initiate_chat", new=AsyncMock()) as mock_initiate, \
         patch("app.agent.loop.manager") as mock_mgr:
        mock_mgr.broadcast = AsyncMock(side_effect=lambda d, **k: broadcasts.append(d))
        tier = await loop._tick_round()

    assert tier == BudgetTier.RULE_ONLY
    assert seen_force and all(seen_force)          # every tick forced plan-only
    mock_initiate.assert_not_called()              # inter-resident chat suppressed
    assert not any(b.get("type") == "resident_chat" for b in broadcasts)


# ── Night homing（burn-in 修复批次 1, Task 3）────────────────────────

@pytest.mark.anyio
async def test_tick_round_night_runs_homing_not_llm(loop_session_factory, loop_residents):
    """活动概率 0（夜间）时：不调 resident_tick，改跑 night_homing_step。"""
    homing = AsyncMock(return_value=(1, 1))
    tick = AsyncMock()
    with patch("app.agent.loop.async_session", loop_session_factory), \
         patch("app.agent.loop.build_schedule", return_value=MagicMock(
             wake_hour=8, sleep_hour=22, peak_hours=[10], social_slots=[], rest_ratio=0.3)), \
         patch("app.agent.loop.get_activity_probability", return_value=0.0), \
         patch("app.agent.loop.night_homing_step", homing), \
         patch("app.agent.loop.resident_tick", tick), \
         patch("app.agent.loop.manager") as mock_manager:
        mock_manager.broadcast = AsyncMock()
        await AgentLoop()._tick_round()

    assert tick.await_count == 0            # 零 LLM tick
    assert homing.await_count >= 1          # 归巢步跑了
    assert mock_manager.broadcast.await_count >= 1   # resident_move 帧广播
    frame = mock_manager.broadcast.await_args_list[0].args[0]
    assert frame["type"] == "resident_move" and frame["status"] == "walking"


@pytest.mark.anyio
async def test_active_trip_bypasses_only_awake_random_gate(
    loop_session_factory, loop_residents, monkeypatch,
):
    """A saved trip advances even when should_tick loses its random roll."""
    from app.agent.plan_continuity import _active_key
    from app.config import settings
    from app.redis_client import get_redis
    monkeypatch.setattr(settings, "realism_plan_continuity_enabled", True)
    active_id = loop_residents[0].id
    await get_redis().set(_active_key(active_id), "existence-hint")
    ticked = []

    async def capture_tick(db, resident):
        ticked.append(resident.id)
        return None

    with patch("app.agent.loop.async_session", loop_session_factory), \
         patch("app.agent.loop.resident_tick", side_effect=capture_tick), \
         patch("app.agent.loop.should_tick", return_value=False) as random_gate, \
         patch("app.agent.loop.get_activity_probability", return_value=0.5), \
         patch("app.agent.loop.build_schedule", return_value=MagicMock(
             wake_hour=6, sleep_hour=23, peak_hours=[10],
             social_slots=[14], rest_ratio=0.3)):
        await AgentLoop()._tick_round()

    assert ticked == [active_id]
    # The other two residents still went through (and failed) the random gate.
    assert random_gate.call_count == len(loop_residents) - 1


@pytest.mark.anyio
async def test_active_trip_does_not_bypass_closed_schedule(
    loop_session_factory, loop_residents, monkeypatch,
):
    from app.agent.plan_continuity import _active_key
    from app.config import settings
    from app.redis_client import get_redis
    monkeypatch.setattr(settings, "realism_plan_continuity_enabled", True)
    await get_redis().set(_active_key(loop_residents[0].id), "existence-hint")
    tick = AsyncMock()
    homing = AsyncMock(return_value=None)

    with patch("app.agent.loop.async_session", loop_session_factory), \
         patch("app.agent.loop.resident_tick", tick), \
         patch("app.agent.loop.night_homing_step", homing), \
         patch("app.agent.loop.should_tick") as random_gate, \
         patch("app.agent.loop.get_activity_probability", return_value=0.0), \
         patch("app.agent.loop.build_schedule", return_value=MagicMock(
             wake_hour=8, sleep_hour=22, peak_hours=[10],
             social_slots=[], rest_ratio=0.3)):
        await AgentLoop()._tick_round()

    tick.assert_not_awaited()
    random_gate.assert_not_called()
    assert homing.await_count == len(loop_residents)


# --------------------------------------------------------------------------- #
# S3: tick skip of chatting/socializing gated behind chat_engaged_tick_skip   #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_default_ticks_chatting_residents(
    db_session, loop_session_factory, loop_residents
):
    """Flag off (default): chatting/socializing residents still tick (master)."""
    loop_residents[0].status = "chatting"
    loop_residents[1].status = "socializing"
    await db_session.commit()
    ticked: list[str] = []

    async def capture_tick(db, resident):
        ticked.append(resident.slug)
        return None

    with patch("app.agent.loop.async_session", loop_session_factory), \
         patch("app.agent.loop.resident_tick", side_effect=capture_tick), \
         patch("app.agent.loop.should_tick", return_value=True), \
         patch("app.agent.loop.get_activity_probability", return_value=0.5), \
         patch("app.agent.loop.build_schedule", return_value=MagicMock(
             wake_hour=6, sleep_hour=23, peak_hours=[10],
             social_slots=[14], rest_ratio=0.3)):
        await AgentLoop()._tick_round()

    assert set(ticked) == {r.slug for r in loop_residents}


@pytest.mark.anyio
async def test_flag_on_skips_locked_chatting(
    db_session, loop_session_factory, loop_residents, monkeypatch
):
    """Flag on: a chatting resident whose Redis chat lock exists is skipped."""
    from app.config import settings
    from app.redis_client import get_redis
    from app.ws.manager import CHATTING_PREFIX

    monkeypatch.setattr(settings, "chat_engaged_tick_skip_enabled", True)
    loop_residents[0].status = "chatting"
    loop_residents[1].status = "socializing"
    await db_session.commit()
    await get_redis().set(CHATTING_PREFIX + loop_residents[0].id, "human-user")
    ticked: list[str] = []

    async def capture_tick(db, resident):
        ticked.append(resident.slug)
        return None

    with patch("app.agent.loop.async_session", loop_session_factory), \
         patch("app.agent.loop.resident_tick", side_effect=capture_tick), \
         patch("app.agent.loop.should_tick", return_value=True), \
         patch("app.agent.loop.get_activity_probability", return_value=0.5), \
         patch("app.agent.loop.build_schedule", return_value=MagicMock(
             wake_hour=6, sleep_hour=23, peak_hours=[10],
             social_slots=[14], rest_ratio=0.3)):
        await AgentLoop()._tick_round()

    # locked chatting + socializing both skipped; only the idle one ticks
    assert ticked == [loop_residents[2].slug]
    # locked chatting resident's status is untouched
    await db_session.refresh(loop_residents[0])
    assert loop_residents[0].status == "chatting"


@pytest.mark.anyio
async def test_flag_on_heals_stale_chatting(
    db_session, loop_session_factory, loop_residents, monkeypatch
):
    """Flag on: chatting without a Redis lock is stale — reset to idle + tick."""
    from app.config import settings

    monkeypatch.setattr(settings, "chat_engaged_tick_skip_enabled", True)
    # The :memory: test engine rides one shared StaticPool connection: a
    # concurrent tick session closing (ROLLBACK) between the heal UPDATE and
    # its COMMIT would discard the write. Serialize ticks so the heal commit
    # is deterministic (prod PG gives each session its own connection).
    monkeypatch.setattr(settings, "agent_max_concurrent", 1)
    loop_residents[0].status = "chatting"
    await db_session.commit()
    ticked: list[str] = []

    async def capture_tick(db, resident):
        ticked.append(resident.slug)
        return None

    with patch("app.agent.loop.async_session", loop_session_factory), \
         patch("app.agent.loop.resident_tick", side_effect=capture_tick), \
         patch("app.agent.loop.should_tick", return_value=True), \
         patch("app.agent.loop.get_activity_probability", return_value=0.5), \
         patch("app.agent.loop.build_schedule", return_value=MagicMock(
             wake_hour=6, sleep_hour=23, peak_hours=[10],
             social_slots=[14], rest_ratio=0.3)):
        await AgentLoop()._tick_round()

    assert set(ticked) == {r.slug for r in loop_residents}
    await db_session.refresh(loop_residents[0])
    assert loop_residents[0].status == "idle"
