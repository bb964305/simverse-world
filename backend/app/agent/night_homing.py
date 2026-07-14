"""Night homing: 作息门关闭后（活动概率 0），居民规则化走回家——零 LLM。

burn-in 发现：sleep_hour 后 should_tick 恒 False，居民冻结在最后位置"就地入睡"。
本模块每 tick 让不在家的居民朝家走一步，与 BasicExecutePlugin 的移动语义一致。
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.map_data import get_valid_target_tile
from app.agent.pathfinder import find_path, get_walkable_tiles
from app.models.resident import Resident

logger = logging.getLogger(__name__)


def _home_target(resident: Resident) -> tuple[int, int] | None:
    if getattr(resident, "home_location_id", None):
        t = get_valid_target_tile(resident.home_location_id)
        if t:
            return (t[0], t[1])
    if resident.home_tile_x is not None and resident.home_tile_y is not None:
        return (resident.home_tile_x, resident.home_tile_y)
    return None


async def night_homing_step(db: AsyncSession, resident: Resident) -> tuple[int, int] | None:
    """Move one tile toward home. Returns the new tile, or None when settled."""
    target = _home_target(resident)
    if target is None:
        return None
    if (resident.tile_x, resident.tile_y) == target:
        if resident.status == "walking":
            resident.status = "idle"
            await db.commit()
        return None
    path = find_path((resident.tile_x, resident.tile_y), target, get_walkable_tiles())
    if not path or len(path) < 2:
        if resident.status == "walking":
            resident.status = "idle"
            await db.commit()
        return None
    nxt = path[1]
    resident.tile_x, resident.tile_y = nxt[0], nxt[1]
    resident.status = "walking"
    await db.commit()
    return (nxt[0], nxt[1])
