"""E8 exploration codex content: location lore + hidden spots (handwritten).

Kept separate from map_data.LOCATIONS so the codex content can grow without
touching the movement/pathfinding data. Lore is shown on first visit; a hidden
spot is one exact secret tile per location that rewards a small bonus.
"""

# location_id -> lore blurb (shown on first visit)
LORE: dict[str, str] = {
    "academy": "学院的走廊里回荡着几代人的读书声，据说地下还藏着一间无人使用的老教室。",
    "library": "这座图书馆的藏书比小镇的历史还长，最深处的书架据说会自己重新排列。",
    "tavern": "酒馆的木桌上刻满了名字，每一道刻痕都是一个再没回来过的旅人。",
    "cafe": "咖啡馆的老板从不记账，却记得每个常客的口味和心事。",
    "workshop": "工坊的角落堆着无数半成品，每一件都曾是某个人未完成的梦想。",
    "shop": "小店什么都卖，据说只要你真心想要，总能在某个货架找到它。",
    "town_hall": "市政厅的钟楼百年未响，居民们都在等它下一次敲响的理由。",
    "central_plaza": "广场中央的喷泉见证了小镇所有的相遇与告别。",
    # Voted into existence by the civic agenda (civic_service.CIVIC_AGENDA);
    # they are dynamic_locations rows, not static LOCATIONS entries.
    "post_office": "邮局的木格柜里塞满了写好却没寄出的信，据说其中有几封的收信人还没出生。",
    "theater": "剧院的座椅还带着新木头的气味，据说散场之后，台上的故事会在空座位间再演一遍。",
}

# location_id -> the single secret tile (x, y)
HIDDEN_SPOTS: dict[str, tuple[int, int]] = {
    "academy": (17, 20),
    "library": (110, 22),
    "tavern": (59, 45),
    "cafe": (78, 45),
    "central_plaza": (110, 24),
}

# reverse: (x, y) -> location_id for O(1) secret detection
SECRET_TILE_TO_LOCATION: dict[tuple[int, int], str] = {t: loc for loc, t in HIDDEN_SPOTS.items()}

SECRET_REWARD_SC = 5

# Dynamic lore merged from approved add_lore proposals (P3 overlay). Refreshed
# by the world-reload path; overrides/extends the handwritten LORE above.
_dynamic_lore: dict[str, str] = {}


async def load_dynamic_lore() -> int:
    """Load active ``lore`` dynamic_mechanics into the in-memory overlay. Called
    at startup / on the sv:world:reload signal. Fail-open (returns 0)."""
    from sqlalchemy import select
    from app.database import async_session
    from app.models.dynamic_mechanic import DynamicMechanic

    global _dynamic_lore
    try:
        async with async_session() as db:
            rows = (await db.execute(
                select(DynamicMechanic).where(
                    DynamicMechanic.kind == "lore", DynamicMechanic.active.is_(True)
                )
            )).scalars().all()
    except Exception:
        return 0
    merged: dict[str, str] = {}
    for r in rows:
        spec = r.spec_json or {}
        loc, text = spec.get("location_id"), spec.get("text")
        if loc and text:
            merged[loc] = text
    _dynamic_lore = merged
    return len(merged)


def lore_for(location_id: str) -> str | None:
    return _dynamic_lore.get(location_id) or LORE.get(location_id)
