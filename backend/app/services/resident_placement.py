"""Resident placement utilities — location normalization, tile allocation, slugs, sprites.

Pipeline-neutral helpers shared by the forge pipelines (legacy + canonical),
resident import, onboarding and seed scripts. Extracted from the deprecated
app/services/forge_service.py (P1-6 file split); behavior is unchanged.
"""

import random
import re
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.map_data import LOCATIONS as _MAP_LOCATIONS, get_location_id_at, allocate_home
from app.models.resident import Resident

_LOCATION_BOUNDS = {
    k: v["bounds"] for k, v in _MAP_LOCATIONS.items()
    if v["type"] not in ("private", "apartment")
}


def _gen_slots(x1: int, y1: int, x2: int, y2: int, step: int = 2) -> list[tuple[int, int]]:
    """Generate a grid of candidate tiles within bounds."""
    return [(x, y) for x in range(x1, x2 + 1, step) for y in range(y1, y2 + 1, step)]


LOCATION_TILE_SLOTS: dict[str, list[tuple[int, int]]] = {
    loc: _gen_slots(*bounds) for loc, bounds in _LOCATION_BOUNDS.items()
}

# Backwards alias: old code uses DISTRICT_TILE_SLOTS
DISTRICT_TILE_SLOTS = LOCATION_TILE_SLOTS

DEFAULT_LOCATION_ID = "central_plaza"
LEGACY_LOCATION_ALIASES: dict[str, str] = {
    "engineering": "workshop",
    "product": "cafe",
    "academy": "academy",
    "free": DEFAULT_LOCATION_ID,
    "outdoor": DEFAULT_LOCATION_ID,
}
VALID_LOCATION_IDS = set(_MAP_LOCATIONS)
ALLOCATABLE_LOCATION_IDS = set(LOCATION_TILE_SLOTS)
_KEYWORD_LOCATION_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("engineer", "backend", "frontend", "algorithm", "code", "编程", "架构", "开发", "制造", "修理", "devops"), "workshop"),
    (("teacher", "professor", "学者", "教授", "导师", "学习", "研究", "哲学", "mentor", "historian"), "academy"),
    (("librarian", "book", "书", "阅读", "知识", "写作", "writer", "author"), "library"),
    (("shop", "store", "卖", "商", "交易", "经济", "money", "retail"), "shop"),
    (("admin", "govern", "市政", "行政", "管理", "政治", "operations"), "town_hall"),
    (("drink", "bar", "酒", "社交", "聚会"), "tavern"),
    (("coffee", "咖啡", "休闲", "放松", "chat", "product", "design", "产品", "设计", "marketing"), "cafe"),
)

SPRITE_KEYS = [
    "伊莎贝拉", "克劳斯", "亚当", "梅", "塔玛拉",
    "亚瑟", "卡洛斯", "弗朗西斯科", "海莉", "拉托亚",
    "詹妮弗", "约翰", "玛丽亚", "沃尔夫冈", "汤姆",
    "山本百合子", "山姆", "乔治", "简", "埃迪",
]


def normalize_location_id(
    value: str | None,
    *,
    default: str = DEFAULT_LOCATION_ID,
    allocatable_only: bool = False,
) -> str:
    """Normalize legacy district labels and unknown values to canonical map location IDs."""
    candidate = (value or "").strip().lower()
    if not candidate:
        candidate = default

    if candidate in LEGACY_LOCATION_ALIASES:
        candidate = LEGACY_LOCATION_ALIASES[candidate]

    valid_values = ALLOCATABLE_LOCATION_IDS if allocatable_only else VALID_LOCATION_IDS
    if candidate not in valid_values:
        candidate = default

    return candidate


def infer_location_id_from_text(*text_parts: str, default: str = DEFAULT_LOCATION_ID) -> str:
    """Infer a canonical location ID from free-form resident descriptions."""
    combined = " ".join(part for part in text_parts if part).lower()
    for keywords, location_id in _KEYWORD_LOCATION_RULES:
        if any(keyword in combined for keyword in keywords):
            return location_id
    return default


async def _is_tile_occupied(db: AsyncSession, tile_x: int, tile_y: int) -> bool:
    result = await db.execute(
        select(Resident.id).where(
            Resident.tile_x == tile_x,
            Resident.tile_y == tile_y,
        )
    )
    return result.scalar_one_or_none() is not None


async def allocate_resident_location(
    db: AsyncSession,
    *,
    requested_location_id: str | None = None,
    preferred_tile: tuple[int, int] | None = None,
    ability_text: str = "",
    persona_text: str = "",
    soul_text: str = "",
    default_location_id: str = DEFAULT_LOCATION_ID,
    assign_housing: bool = True,
) -> tuple[str, int, int, str | None]:
    """Resolve a resident creation request to one canonical location ID and one tile.

    Returns:
        (location_id, tile_x, tile_y, home_location_id)
        home_location_id is None if assign_housing=False or all slots are full.
    """
    canonical_location_id = normalize_location_id(
        requested_location_id,
        default=default_location_id,
        allocatable_only=True,
    )
    if requested_location_id is None:
        canonical_location_id = infer_location_id_from_text(
            ability_text,
            persona_text,
            soul_text,
            default=default_location_id,
        )

    if preferred_tile is not None:
        preferred_location_id = get_location_id_at(*preferred_tile)
        if preferred_location_id:
            canonical_location_id = normalize_location_id(preferred_location_id, default=canonical_location_id, allocatable_only=True)
            if not await _is_tile_occupied(db, *preferred_tile):
                home_id = await allocate_home(db) if assign_housing else None
                return canonical_location_id, preferred_tile[0], preferred_tile[1], home_id

    tile_x, tile_y = await _find_available_tile(db, canonical_location_id)
    home_id = await allocate_home(db) if assign_housing else None
    return canonical_location_id, tile_x, tile_y, home_id


async def _find_available_tile(db: AsyncSession, district: str) -> tuple[int, int]:
    district = normalize_location_id(district, allocatable_only=True)
    slots = LOCATION_TILE_SLOTS.get(district, LOCATION_TILE_SLOTS.get(DEFAULT_LOCATION_ID, []))
    result = await db.execute(
        select(Resident.tile_x, Resident.tile_y).where(Resident.district == district)
    )
    occupied = {(row.tile_x, row.tile_y) for row in result.all()}
    for x, y in slots:
        if (x, y) not in occupied:
            return x, y
    base_x, base_y = slots[-1]
    return base_x + random.randint(1, 5) * 2, base_y + random.randint(1, 5) * 2


def _generate_slug(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r'[^\w\u4e00-\u9fff-]', '-', slug)
    slug = re.sub(r'-+', '-', slug).strip('-')
    if not slug:
        slug = f"resident-{uuid.uuid4().hex[:8]}"
    return slug
