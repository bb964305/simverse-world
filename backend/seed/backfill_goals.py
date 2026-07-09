"""A1: backfill a life goal for every resident that lacks one.

Run: python -m seed.backfill_goals

Uses a rule-based template (zero LLM) so it's safe to run in any environment
(vm212's AGENT_ENABLED=false). Richer LLM-generated goals can replace these
later by re-running with an LLM-backed generator.
"""
import asyncio

from sqlalchemy import select

from app.database import async_session
from app.models.resident import Resident
from app.services.goal_service import get_active_goal, create_goal

TEMPLATES = {
    "workshop": ("打造一件传世的作品", "把毕生所学凝聚成一件真正有价值的东西"),
    "academy": ("写一本影响后人的书", "把自己的思考留给这个世界"),
    "library": ("读遍这座城的每一本书", "在书里寻找自己存在的意义"),
    "cafe": ("在自由区开一家温暖的咖啡馆", "想给疲惫的人一个歇脚的地方"),
    "tavern": ("成为小镇最受欢迎的主人", "喜欢看到大家聚在一起的样子"),
    "shop": ("把小店做成大家离不开的地方", "被人需要是一种幸福"),
    "town_hall": ("让这座小镇变得更好", "肩上有一份不能放下的责任"),
}
DEFAULT = ("找到属于自己的位置", "还在寻找生活的方向")


async def main() -> None:
    created = 0
    async with async_session() as db:
        residents = (await db.execute(select(Resident))).scalars().all()
        for r in residents:
            if await get_active_goal(db, r.id):
                continue
            title, motivation = TEMPLATES.get(r.district, DEFAULT)
            await create_goal(db, r.id, title, motivation)
            created += 1
    print(f"Backfilled {created} resident goals.")


if __name__ == "__main__":
    asyncio.run(main())
