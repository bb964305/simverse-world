"""A1 life goals: create, query, and the weekly LLM evaluation.

The weekly evaluation reads the resident's recent important memories, asks the
system-channel LLM for a JSON verdict, and advances the goal. On achieved/failed
it writes a high-importance reflection memory (which feeds the personality-shift
evaluator naturally on the next tick).
"""

import logging
from datetime import datetime, timedelta, UTC

from sqlalchemy import select

from app.config import settings
from app.llm.client import get_client
from app.llm.json_extract import extract_json_object
from app.llm.metering import record_usage
from app.models.memory import Memory
from app.models.resident import Resident
from app.models.resident_goal import ResidentGoal

logger = logging.getLogger(__name__)

EVAL_SYSTEM = (
    "你是角色成长评估器。根据该角色本周的记忆，评估他/她的人生目标进展。"
    '只输出 JSON：{"progress_delta": 0.0~0.3, "milestone": "本周达成的小里程碑或null", '
    '"verdict": "none|achieved|failed"}。'
)


def serialize(g: ResidentGoal) -> dict:
    return {
        "id": g.id,
        "kind": g.kind,
        "title": g.title,
        "motivation": g.motivation,
        "status": g.status,
        "progress": round(g.progress, 3),
        "milestones": g.milestones_json or [],
        "resolved_at": g.resolved_at.isoformat() if g.resolved_at else None,
    }


async def create_goal(db, resident_id, title, motivation="", kind="life") -> ResidentGoal:
    """Create a goal; if a life goal, deactivate any existing active life goal."""
    if kind == "life":
        existing = (await db.execute(
            select(ResidentGoal).where(
                ResidentGoal.resident_id == resident_id,
                ResidentGoal.kind == "life", ResidentGoal.status == "active",
            )
        )).scalars().all()
        for g in existing:
            g.status = "abandoned"
    goal = ResidentGoal(resident_id=resident_id, kind=kind, title=title, motivation=motivation, status="active")
    db.add(goal)
    await db.commit()
    await db.refresh(goal)
    return goal


async def get_active_goal(db, resident_id) -> ResidentGoal | None:
    return (await db.execute(
        select(ResidentGoal).where(
            ResidentGoal.resident_id == resident_id,
            ResidentGoal.kind == "life", ResidentGoal.status == "active",
        ).order_by(ResidentGoal.created_at.desc())
    )).scalars().first()


async def get_goals(db, resident_id) -> dict:
    active = await get_active_goal(db, resident_id)
    resolved = (await db.execute(
        select(ResidentGoal).where(
            ResidentGoal.resident_id == resident_id,
            ResidentGoal.status.in_(["achieved", "failed"]),
        ).order_by(ResidentGoal.resolved_at.desc()).limit(3)
    )).scalars().all()
    return {
        "active": serialize(active) if active else None,
        "resolved": [serialize(g) for g in resolved],
    }


async def weekly_evaluate(db, resident_id) -> ResidentGoal | None:
    """Advance the resident's active life goal based on this week's memories."""
    goal = await get_active_goal(db, resident_id)
    if goal is None:
        return None

    week_ago = datetime.now(UTC) - timedelta(days=7)
    mems = (await db.execute(
        select(Memory.content).where(
            Memory.resident_id == resident_id,
            Memory.importance >= 0.5,
            Memory.created_at >= week_ago,
        ).order_by(Memory.importance.desc()).limit(30)
    )).scalars().all()

    material = "\n".join(f"- {m}" for m in mems) or "（本周无重要记忆）"
    prompt = f"人生目标：{goal.title}\n当前进度：{goal.progress:.0%}\n\n本周记忆：\n{material}"

    client = get_client("system")
    model = settings.effective_model
    resp = await client.messages.create(
        model=model, max_tokens=300, system=EVAL_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )
    text = ""
    for block in resp.content:
        if hasattr(block, "text"):
            text = block.text
            break
    data = extract_json_object(text) or {}
    await record_usage("goal_eval", model=model, owner="system", response=resp, parse_ok=bool(data))

    delta = max(0.0, min(0.3, float(data.get("progress_delta", 0) or 0)))
    goal.progress = min(1.0, goal.progress + delta)
    milestone = data.get("milestone")
    if milestone and milestone not in ("null", "None", ""):
        goal.milestones_json = [*(goal.milestones_json or []), {
            "title": milestone, "done": True, "at": datetime.now(UTC).isoformat(),
        }]
    goal.updated_at = datetime.now(UTC)

    verdict = data.get("verdict", "none")
    if verdict in ("achieved", "failed"):
        goal.status = verdict
        goal.resolved_at = datetime.now(UTC)
        if verdict == "achieved":
            goal.progress = 1.0
        await _on_resolved(db, resident_id, goal, verdict)

    await db.commit()
    await db.refresh(goal)
    return goal


async def _on_resolved(db, resident_id, goal, verdict) -> None:
    """Achieved/failed side effects: a high-importance reflection memory (which
    feeds the personality-shift evaluator on the next tick)."""
    from app.memory.service import MemoryService
    verb = "实现了" if verdict == "achieved" else "没能实现"
    await MemoryService(db).add_memory(
        resident_id, "reflection", f"我{verb}我的人生目标「{goal.title}」，这让我对自己有了新的认识。",
        importance=0.9, source="reflection",
    )
    # A4 bulletin post / E13 settlement / E11 feed hook in here once those land.
