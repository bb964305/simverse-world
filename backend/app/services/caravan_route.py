"""Deterministic caravan routing and clearance audit helpers.

The caravan uses a 1x1 tile navigation anchor so it can reuse the existing
resident pathfinder without changing the movement state machine. This module
adds a stricter, auditable physical-path layer on top of the logical
``get_reachable_tiles()`` result:

* waypoints are chosen from collision-open tiles only;
* the anchor route is computed over the physically reachable component from the
  market hall's loading bay;
* a separate report flags where a 64 px visual overhang may clip nearby
  collisions and whether the route offers a conservative 2-tile-wide corridor.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from math import ceil

from app.agent.map_data import LOCATIONS, get_location_id_at, get_valid_target_tile
from app.agent.pathfinder import (
    _load_collision_tiles,
    find_path,
    get_reachable_tiles,
)
from app.world_geometry import (
    MAP_HEIGHT_TILES,
    MAP_WIDTH_TILES,
    TILE_SIZE,
    WALKABLE_X_RANGE,
    WALKABLE_Y_RANGE,
)

Tile = tuple[int, int]

_TOWN_ENTRANCE_ID = "town_entrance"
_MARKET_HALL_ID = "market_hall"
_VISUAL_OVERHANG_PX = 64
_OUTSIDE_STAGING_ROWS = 8
_MARKET_AVENUE_X_BOUNDS = (100, 104)
_MARKET_AVENUE_Y_BOUNDS = (58, MAP_HEIGHT_TILES - 1)
_MARKET_AVENUE_CENTER_X = 102
_MARKET_HALL_BRANCH_X_BOUNDS = (102, 109)
_MARKET_HALL_BRANCH_Y_BOUNDS = (92, 96)
_MARKET_HALL_LANE_Y = 94


@dataclass(frozen=True)
class CaravanTurn:
    tile: Tile
    from_direction: str
    to_direction: str


@dataclass(frozen=True)
class CaravanClearanceReport:
    anchor_footprint: str
    visual_overhang_px: int
    overhang_radius_tiles: int
    risky_tiles: tuple[Tile, ...]
    min_blocked_chebyshev_distance: int | None
    two_tile_width_supported: bool
    two_tile_width_blockers: tuple[Tile, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class CaravanRoutePlan:
    outside_staging: Tile
    entrance_staging: Tile
    market_hall_parking: Tile
    approach_path: tuple[Tile, ...]
    market_hall_path: tuple[Tile, ...]
    full_path: tuple[Tile, ...]
    step_directions: tuple[str, ...]
    turns: tuple[CaravanTurn, ...]
    anchor_reachable: bool
    clearance: CaravanClearanceReport
    risks: tuple[str, ...]

    # Backward-compatible names for any operator tooling that serialized route
    # plans before the market became a standalone building.
    @property
    def plaza_parking(self) -> Tile:
        return self.market_hall_parking

    @property
    def plaza_path(self) -> tuple[Tile, ...]:
        return self.market_hall_path

    def to_dict(self) -> dict[str, object]:
        return {
            "outside_staging": list(self.outside_staging),
            "entrance_staging": list(self.entrance_staging),
            "market_hall_parking": list(self.market_hall_parking),
            "plaza_parking": list(self.market_hall_parking),
            "approach_path": [list(tile) for tile in self.approach_path],
            "market_hall_path": [list(tile) for tile in self.market_hall_path],
            "plaza_path": [list(tile) for tile in self.market_hall_path],
            "full_path": [list(tile) for tile in self.full_path],
            "step_directions": list(self.step_directions),
            "turns": [
                {
                    "tile": list(turn.tile),
                    "from_direction": turn.from_direction,
                    "to_direction": turn.to_direction,
                }
                for turn in self.turns
            ],
            "anchor_reachable": self.anchor_reachable,
            "clearance": {
                "anchor_footprint": self.clearance.anchor_footprint,
                "visual_overhang_px": self.clearance.visual_overhang_px,
                "overhang_radius_tiles": self.clearance.overhang_radius_tiles,
                "risky_tiles": [list(tile) for tile in self.clearance.risky_tiles],
                "min_blocked_chebyshev_distance": self.clearance.min_blocked_chebyshev_distance,
                "two_tile_width_supported": self.clearance.two_tile_width_supported,
                "two_tile_width_blockers": [
                    list(tile) for tile in self.clearance.two_tile_width_blockers
                ],
                "notes": list(self.clearance.notes),
            },
            "risks": list(self.risks),
        }


@lru_cache(maxsize=1)
def build_caravan_route() -> CaravanRoutePlan:
    """Return the deterministic caravan route from south staging to market hall."""
    # get_reachable_tiles() returns its cached set directly; copy it before
    # adding the caravan-only off-map tail so resident navigation is never
    # polluted by this route build.
    logical_reachable = set(get_reachable_tiles())
    physical_open = _physical_open_tiles()
    # Residents intentionally stop four rows before the decorative map edge.
    # A departing caravan must instead continue along the authored avenue until
    # its body is almost off-screen, so only that explicit exit strip is added
    # to the logical component.
    logical_reachable |= {
        tile for tile in physical_open
        if _on_market_road(tile) and tile[1] > max(WALKABLE_Y_RANGE)
    }
    semantic_open = {
        tile for tile in _semantic_open_tiles(physical_open)
        if _on_market_road(tile)
    }

    market_hall_parking = _select_market_hall_parking(semantic_open, logical_reachable)
    physical_reachable = _reachable_from(market_hall_parking, semantic_open)
    effective_walkable = physical_reachable & logical_reachable
    if market_hall_parking not in effective_walkable:
        raise RuntimeError("market_hall parking tile is not physically reachable")

    entrance_staging = _select_entrance_staging(effective_walkable, market_hall_parking)
    outside_staging = _select_outside_staging(effective_walkable, entrance_staging)
    centerline_walkable = effective_walkable & _market_centerline_tiles()

    approach_path = _path_or_raise(
        outside_staging,
        entrance_staging,
        centerline_walkable,
        "outside->entrance",
    )
    market_hall_path = _path_or_raise(
        entrance_staging,
        market_hall_parking,
        centerline_walkable,
        "entrance->market_hall",
    )
    full_path = tuple(approach_path + list(market_hall_path[1:]))

    step_directions = _path_directions(full_path)
    turns = _path_turns(full_path, step_directions)
    clearance = _audit_clearance(full_path, physical_open)

    risks: list[str] = []
    if clearance.risky_tiles:
        distance = clearance.min_blocked_chebyshev_distance
        risks.append(
            "64px visual overhang approaches collision tiles within "
            f"{distance} tile(s) on {len(clearance.risky_tiles)} anchor positions"
        )
    if not clearance.two_tile_width_supported:
        risks.append(
            "No continuous audited 2-tile corridor; use a 1x1 anchor and treat the caravan body as visual overhang"
        )

    return CaravanRoutePlan(
        outside_staging=outside_staging,
        entrance_staging=entrance_staging,
        market_hall_parking=market_hall_parking,
        approach_path=tuple(approach_path),
        market_hall_path=tuple(market_hall_path),
        full_path=full_path,
        step_directions=step_directions,
        turns=turns,
        anchor_reachable=all(tile in effective_walkable for tile in full_path),
        clearance=clearance,
        risks=tuple(risks),
    )


def _path_or_raise(
    start: Tile,
    end: Tile,
    walkable_tiles: set[Tile],
    label: str,
) -> list[Tile]:
    path = find_path(start, end, walkable_tiles)
    if not path:
        raise RuntimeError(f"Could not resolve caravan path for {label}")
    return path


def _select_market_hall_parking(physical_open: set[Tile], logical_reachable: set[Tile]) -> Tile:
    bounds = LOCATIONS[_MARKET_HALL_ID]["bounds"]
    center = tuple(
        LOCATIONS[_MARKET_HALL_ID].get("caravan_parking")
        or get_valid_target_tile(_MARKET_HALL_ID)
    )
    candidates = _tiles_in_bounds(bounds, physical_open & logical_reachable)
    if not center or not candidates:
        raise RuntimeError("No physical market_hall parking candidates are available")
    if center in candidates:
        return center
    return min(
        candidates,
        key=lambda tile: (
            _manhattan(tile, center),
            -_open_neighborhood_score(tile, physical_open, radius=1),
            tile[1],
            tile[0],
        ),
    )


def _select_entrance_staging(effective_walkable: set[Tile], parking_tile: Tile) -> Tile:
    bounds = LOCATIONS[_TOWN_ENTRANCE_ID]["bounds"]
    center = get_valid_target_tile(_TOWN_ENTRANCE_ID)
    candidates = _tiles_in_bounds(bounds, effective_walkable)
    if center in candidates and find_path(center, parking_tile, effective_walkable):
        return center
    ranked: list[tuple[int, int, int, int, Tile]] = []
    for tile in candidates:
        path = find_path(tile, parking_tile, effective_walkable)
        if not path:
            continue
        ranked.append(
            (
                _manhattan(tile, center),
                len(path),
                tile[1],
                tile[0],
                tile,
            )
        )
    if not ranked:
        raise RuntimeError("No inside staging tile can reach market_hall parking")
    return min(ranked)[-1]


def _select_outside_staging(effective_walkable: set[Tile], entrance_tile: Tile) -> Tile:
    x1, _, x2, y2 = LOCATIONS[_TOWN_ENTRANCE_ID]["bounds"]
    center = get_valid_target_tile(_TOWN_ENTRANCE_ID)
    desired = (center[0], min(MAP_HEIGHT_TILES - 1, y2 + _OUTSIDE_STAGING_ROWS))
    candidates = sorted(
        tile
        for tile in effective_walkable
        if x1 <= tile[0] <= x2 and y2 < tile[1] <= min(MAP_HEIGHT_TILES - 1, y2 + _OUTSIDE_STAGING_ROWS)
    )
    ranked: list[tuple[int, int, int, int, Tile]] = []
    for tile in candidates:
        path = find_path(tile, entrance_tile, effective_walkable)
        if not path:
            continue
        ranked.append(
            (
                _manhattan(tile, desired),
                abs(tile[0] - center[0]),
                len(path),
                tile[1],
                tile[0],
                tile,
            )
        )
    if not ranked:
        raise RuntimeError("No outside staging tile can reach the town entrance staging area")
    return min(ranked)[-1]


def _tiles_in_bounds(bounds: tuple[int, int, int, int], tiles: set[Tile]) -> tuple[Tile, ...]:
    x1, y1, x2, y2 = bounds
    return tuple(sorted(tile for tile in tiles if x1 <= tile[0] <= x2 and y1 <= tile[1] <= y2))


def _physical_open_tiles() -> set[Tile]:
    blocked = _load_collision_tiles()
    tiles: set[Tile] = set()
    # The caravan owns a painted exit corridor all the way to the southern map
    # edge.  The generic resident loader deliberately crops the outer border;
    # the route mask below prevents this wider physical scan from admitting any
    # other decorative-border tile.
    for x in range(MAP_WIDTH_TILES):
        for y in range(MAP_HEIGHT_TILES):
            if (x, y) not in blocked:
                tiles.add((x, y))
    return tiles


def _semantic_open_tiles(physical_open: set[Tile]) -> set[Tile]:
    """Caravans use outdoor roads plus the market hall's dedicated loading aisle."""
    return {tile for tile in physical_open if _caravan_tile_allowed(tile)}


def _caravan_tile_allowed(tile: Tile) -> bool:
    loc_id = get_location_id_at(*tile)
    if loc_id is None:
        return True
    return loc_id == _MARKET_HALL_ID or LOCATIONS.get(loc_id, {}).get("type") == "outdoor"


def _on_market_road(tile: Tile) -> bool:
    """Whether a tile belongs to the authored, visibly paved caravan route."""
    x, y = tile
    avenue_x1, avenue_x2 = _MARKET_AVENUE_X_BOUNDS
    avenue_y1, avenue_y2 = _MARKET_AVENUE_Y_BOUNDS
    branch_x1, branch_x2 = _MARKET_HALL_BRANCH_X_BOUNDS
    branch_y1, branch_y2 = _MARKET_HALL_BRANCH_Y_BOUNDS
    return (
        avenue_x1 <= x <= avenue_x2 and avenue_y1 <= y <= avenue_y2
    ) or (
        branch_x1 <= x <= branch_x2 and branch_y1 <= y <= branch_y2
    )


def _market_centerline_tiles() -> set[Tile]:
    """The wagon anchor follows the middle of the five-wide paved route.

    Ordinary A* is intentionally not allowed to choose among equally short
    edge tiles: doing so made the 64px wagon hug trees/buildings and produced a
    six-turn zig-zag despite the straight boulevard.  The road body remains
    five tiles wide for clearance, while the durable motion path has one turn.
    """
    return {
        *(
            (_MARKET_AVENUE_CENTER_X, y)
            for y in range(_MARKET_HALL_LANE_Y, MAP_HEIGHT_TILES)
        ),
        *(
            (x, _MARKET_HALL_LANE_Y)
            for x in range(_MARKET_AVENUE_CENTER_X, _MARKET_HALL_BRANCH_X_BOUNDS[1] + 1)
        ),
    }


def _reachable_from(seed: Tile, open_tiles: set[Tile]) -> set[Tile]:
    if seed not in open_tiles:
        return set()
    seen = {seed}
    queue = deque([seed])
    while queue:
        x, y = queue.popleft()
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            neighbor = (x + dx, y + dy)
            if neighbor in open_tiles and neighbor not in seen:
                seen.add(neighbor)
                queue.append(neighbor)
    return seen


def _open_neighborhood_score(tile: Tile, open_tiles: set[Tile], *, radius: int) -> int:
    x, y = tile
    return sum(
        1
        for dx in range(-radius, radius + 1)
        for dy in range(-radius, radius + 1)
        if (x + dx, y + dy) in open_tiles
    )


def _manhattan(a: Tile, b: Tile) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _path_directions(path: tuple[Tile, ...]) -> tuple[str, ...]:
    directions: list[str] = []
    for current, nxt in zip(path, path[1:]):
        dx = nxt[0] - current[0]
        dy = nxt[1] - current[1]
        if (dx, dy) == (1, 0):
            directions.append("east")
        elif (dx, dy) == (-1, 0):
            directions.append("west")
        elif (dx, dy) == (0, 1):
            directions.append("south")
        elif (dx, dy) == (0, -1):
            directions.append("north")
        else:
            raise ValueError(f"Non-cardinal caravan step: {current} -> {nxt}")
    return tuple(directions)


def _path_turns(path: tuple[Tile, ...], directions: tuple[str, ...]) -> tuple[CaravanTurn, ...]:
    turns: list[CaravanTurn] = []
    for idx in range(1, len(directions)):
        if directions[idx] != directions[idx - 1]:
            turns.append(
                CaravanTurn(
                    tile=path[idx],
                    from_direction=directions[idx - 1],
                    to_direction=directions[idx],
                )
            )
    return tuple(turns)


def _audit_clearance(path: tuple[Tile, ...], physical_open: set[Tile]) -> CaravanClearanceReport:
    radius = ceil(_VISUAL_OVERHANG_PX / TILE_SIZE)
    risky_tiles: list[Tile] = []
    min_distance: int | None = None
    for tile in path:
        distance = _nearest_blocked_chebyshev(tile, physical_open, search_radius=radius)
        if distance is None:
            continue
        min_distance = distance if min_distance is None else min(min_distance, distance)
        if distance <= radius:
            risky_tiles.append(tile)

    width_2_supported, width_2_blockers = _audit_two_tile_width(path, physical_open)

    notes: list[str] = [
        "Anchor route is audited on collision-open tiles only",
        f"Visual body audit uses a {radius}-tile ({_VISUAL_OVERHANG_PX}px) Chebyshev envelope around the 1x1 anchor",
    ]
    if not width_2_supported:
        notes.append(
            "2-tile width support is conservative: every segment needs an adjacent strip and every turn needs a free 2x2 corner box"
        )

    return CaravanClearanceReport(
        anchor_footprint="1x1",
        visual_overhang_px=_VISUAL_OVERHANG_PX,
        overhang_radius_tiles=radius,
        risky_tiles=tuple(risky_tiles),
        min_blocked_chebyshev_distance=min_distance,
        two_tile_width_supported=width_2_supported,
        two_tile_width_blockers=width_2_blockers,
        notes=tuple(notes),
    )


def _nearest_blocked_chebyshev(tile: Tile, open_tiles: set[Tile], *, search_radius: int) -> int | None:
    x, y = tile
    nearest: int | None = None
    for dx in range(-search_radius, search_radius + 1):
        for dy in range(-search_radius, search_radius + 1):
            if dx == 0 and dy == 0:
                continue
            nx = x + dx
            ny = y + dy
            if not _in_anchor_space(nx, ny) or (nx, ny) not in open_tiles:
                distance = max(abs(dx), abs(dy))
                nearest = distance if nearest is None else min(nearest, distance)
    return nearest


def _audit_two_tile_width(path: tuple[Tile, ...], physical_open: set[Tile]) -> tuple[bool, tuple[Tile, ...]]:
    blockers: set[Tile] = set()
    for current, nxt in zip(path, path[1:]):
        if not _segment_has_adjacent_strip(current, nxt, physical_open):
            blockers.add(current)
            blockers.add(nxt)
    for idx in range(1, len(path) - 1):
        prev_dir = _direction_delta(path[idx - 1], path[idx])
        next_dir = _direction_delta(path[idx], path[idx + 1])
        if prev_dir != next_dir and not _turn_has_corner_box(path[idx], physical_open):
            blockers.add(path[idx])
    return not blockers, tuple(sorted(blockers))


def _segment_has_adjacent_strip(current: Tile, nxt: Tile, physical_open: set[Tile]) -> bool:
    dx, dy = _direction_delta(current, nxt)
    if dx:
        offsets = ((0, -1), (0, 1))
    else:
        offsets = ((-1, 0), (1, 0))
    for ox, oy in offsets:
        if (
            (current[0] + ox, current[1] + oy) in physical_open
            and (nxt[0] + ox, nxt[1] + oy) in physical_open
        ):
            return True
    return False


def _turn_has_corner_box(tile: Tile, physical_open: set[Tile]) -> bool:
    x, y = tile
    quadrants = (
        ((x, y), (x + 1, y), (x, y + 1), (x + 1, y + 1)),
        ((x, y), (x - 1, y), (x, y + 1), (x - 1, y + 1)),
        ((x, y), (x + 1, y), (x, y - 1), (x + 1, y - 1)),
        ((x, y), (x - 1, y), (x, y - 1), (x - 1, y - 1)),
    )
    return any(all(member in physical_open for member in quad) for quad in quadrants)


def _direction_delta(current: Tile, nxt: Tile) -> Tile:
    dx = nxt[0] - current[0]
    dy = nxt[1] - current[1]
    if abs(dx) + abs(dy) != 1:
        raise ValueError(f"Expected cardinal step, got {current} -> {nxt}")
    return dx, dy


def _in_anchor_space(x: int, y: int) -> bool:
    return 0 <= x < MAP_WIDTH_TILES and 0 <= y < MAP_HEIGHT_TILES


__all__ = [
    "CaravanClearanceReport",
    "CaravanRoutePlan",
    "CaravanTurn",
    "build_caravan_route",
]
