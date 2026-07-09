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


def lore_for(location_id: str) -> str | None:
    return LORE.get(location_id)
