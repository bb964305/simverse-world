"""A1 life goals: create/query, weekly LLM evaluation, achieved side effects, prompt, API."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.models.resident import Resident
from app.models.resident_goal import ResidentGoal
from app.models.memory import Memory


async def _resident(db, slug="klaus"):
    r = Resident(slug=slug, name="克劳斯", creator_id="system",
                 district="cafe", status="idle", tile_x=1, tile_y=1)
    db.add(r)
    await db.commit()
    return r


def _mock_client(json_text):
    client = MagicMock()
    resp = MagicMock()
    resp.content = [MagicMock(text=json_text)]
    client.messages.create = AsyncMock(return_value=resp)
    return client


@pytest.mark.anyio
async def test_create_and_get_active_goal(db_session):
    from app.services import goal_service as gs
    res = await _resident(db_session)
    goal = await gs.create_goal(db_session, res.id, "开一家咖啡馆", "热爱咖啡")
    assert goal.status == "active" and goal.kind == "life"

    active = await gs.get_active_goal(db_session, res.id)
    assert active.id == goal.id

    # A new life goal deactivates the old one.
    goal2 = await gs.create_goal(db_session, res.id, "环游世界", "想看更大的世界")
    await db_session.refresh(goal)
    assert goal.status == "abandoned"
    assert (await gs.get_active_goal(db_session, res.id)).id == goal2.id


@pytest.mark.anyio
async def test_weekly_eval_accumulates_progress(db_session):
    from app.services import goal_service as gs
    res = await _resident(db_session)
    await gs.create_goal(db_session, res.id, "开一家咖啡馆", "热爱咖啡")

    with patch.object(gs, "get_client", return_value=_mock_client(
             '{"progress_delta": 0.2, "milestone": "找到了店面", "verdict": "none"}')), \
         patch.object(gs, "record_usage", new_callable=AsyncMock):
        g = await gs.weekly_evaluate(db_session, res.id)

    assert abs(g.progress - 0.2) < 1e-6 and g.status == "active"
    assert len(g.milestones_json) == 1 and g.milestones_json[0]["title"] == "找到了店面"


@pytest.mark.anyio
async def test_weekly_eval_achieved_writes_reflection(db_session):
    from app.services import goal_service as gs
    res = await _resident(db_session)
    await gs.create_goal(db_session, res.id, "开一家咖啡馆", "热爱咖啡")

    with patch.object(gs, "get_client", return_value=_mock_client(
             '{"progress_delta": 0.1, "milestone": null, "verdict": "achieved"}')), \
         patch.object(gs, "record_usage", new_callable=AsyncMock):
        g = await gs.weekly_evaluate(db_session, res.id)

    assert g.status == "achieved" and g.progress == 1.0 and g.resolved_at is not None
    refl = (await db_session.execute(
        select(Memory).where(Memory.resident_id == res.id, Memory.source == "reflection")
    )).scalars().all()
    assert len(refl) == 1 and refl[0].importance == 0.9


def test_dialogue_prompt_includes_goal():
    from app.llm.prompt import assemble_system_prompt
    resident = Resident(slug="r1", name="小明", district="cafe", status="idle",
                        tile_x=0, tile_y=0, soul_md="", persona_md="", ability_md="")
    prompt = assemble_system_prompt(resident, life_goal={"title": "开一家咖啡馆", "progress": 0.5})
    assert "人生目标" in prompt and "开一家咖啡馆" in prompt


@pytest.mark.anyio
async def test_goals_api(client, db_session):
    from app.services import goal_service as gs
    res = await _resident(db_session)
    await gs.create_goal(db_session, res.id, "开一家咖啡馆", "热爱咖啡")

    resp = await client.get("/residents/klaus/goals")
    assert resp.status_code == 200
    assert resp.json()["active"]["title"] == "开一家咖啡馆"


@pytest.mark.anyio
async def test_daily_plan_prompt_includes_life_goal(db_session):
    """A1 wrap-up: plan generation injects the active life goal (fail-open)."""
    from unittest.mock import AsyncMock as _AsyncMock
    from app.agent.phases.plan.basic import BasicPlanPlugin
    from app.agent.schemas import TickContext
    from app.services import goal_service as gs

    res = await _resident(db_session)
    await gs.create_goal(db_session, res.id, "开一家咖啡馆", "热爱咖啡")

    llm_response = (
        '{"goal": {"goal": "筹备咖啡馆", "motivation": "梦想"}, "plans": '
        '[{"slot": 0, "hour_range": [7, 9], "action": "IDLE", "target": null, '
        '"location": "home", "importance": 2, "reason": "休息"}]}'
    )
    ctx = TickContext(db=db_session, resident=res, world_time="10:00", hour=10, schedule_phase="上午")

    with patch("app.agent.phases.plan.basic.llm_chat", return_value=llm_response) as mock_llm, \
         patch("app.agent.phases.plan.basic.manager") as mock_mgr:
        mock_mgr.broadcast = _AsyncMock()
        plugin = BasicPlanPlugin(params={"hourly_slots": 1})
        ctx = await plugin.execute(ctx)

    user_prompt = mock_llm.call_args[0][1][0]["content"]
    assert "你的长期目标" in user_prompt and "开一家咖啡馆" in user_prompt
    assert ctx.life_goal == {"title": "开一家咖啡馆", "progress": 0.0}


@pytest.mark.anyio
async def test_daily_plan_without_goal_omits_line(db_session):
    """No active goal → no goal line, and generation still succeeds (fail-open)."""
    from unittest.mock import AsyncMock as _AsyncMock
    from app.agent.phases.plan.basic import BasicPlanPlugin
    from app.agent.schemas import TickContext

    res = await _resident(db_session)
    llm_response = (
        '{"goal": {"goal": "散步", "motivation": "放松"}, "plans": '
        '[{"slot": 0, "hour_range": [7, 9], "action": "IDLE", "target": null, '
        '"location": "home", "importance": 2, "reason": "休息"}]}'
    )
    ctx = TickContext(db=db_session, resident=res, world_time="10:00", hour=10, schedule_phase="上午")

    with patch("app.agent.phases.plan.basic.llm_chat", return_value=llm_response) as mock_llm, \
         patch("app.agent.phases.plan.basic.manager") as mock_mgr:
        mock_mgr.broadcast = _AsyncMock()
        plugin = BasicPlanPlugin(params={"hourly_slots": 1})
        ctx = await plugin.execute(ctx)

    user_prompt = mock_llm.call_args[0][1][0]["content"]
    assert "你的长期目标" not in user_prompt
    assert ctx.life_goal is None
    assert res.daily_goal_json["goal"] == "散步"


@pytest.mark.anyio
async def test_achieved_goal_creates_bulletin_post(db_session):
    """A1 wrap-up: achieved verdict posts to the bulletin board (not pinned)."""
    from app.models.bulletin_post import BulletinPost
    from app.services import goal_service as gs

    res = await _resident(db_session)
    await gs.create_goal(db_session, res.id, "开一家咖啡馆", "热爱咖啡")

    with patch.object(gs, "get_client", return_value=_mock_client(
             '{"progress_delta": 0.1, "milestone": null, "verdict": "achieved"}')), \
         patch.object(gs, "record_usage", new_callable=AsyncMock):
        await gs.weekly_evaluate(db_session, res.id)

    posts = (await db_session.execute(select(BulletinPost))).scalars().all()
    assert len(posts) == 1
    p = posts[0]
    assert p.kind == "notice" and p.pinned is False
    assert p.title == "克劳斯 实现了人生目标"
    assert "开一家咖啡馆" in p.content_md
    assert p.author_resident_id == res.id


@pytest.mark.anyio
async def test_failed_goal_no_bulletin_post(db_session):
    """Failed verdict resolves the goal but stays off the bulletin board."""
    from app.models.bulletin_post import BulletinPost
    from app.services import goal_service as gs
    from sqlalchemy import func

    res = await _resident(db_session)
    await gs.create_goal(db_session, res.id, "开一家咖啡馆", "热爱咖啡")

    with patch.object(gs, "get_client", return_value=_mock_client(
             '{"progress_delta": 0.0, "milestone": null, "verdict": "failed"}')), \
         patch.object(gs, "record_usage", new_callable=AsyncMock):
        g = await gs.weekly_evaluate(db_session, res.id)

    assert g.status == "failed"
    n = (await db_session.execute(select(func.count()).select_from(BulletinPost))).scalar()
    assert n == 0
