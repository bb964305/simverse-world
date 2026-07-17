import pytest
from app.agent.pathfinder import find_path, get_walkable_tiles


def test_find_path_direct_neighbors():
    """Adjacent tiles should return a 2-step path."""
    walkable = {(x, y) for x in range(10) for y in range(10)}
    path = find_path((0, 0), (1, 0), walkable)
    assert path is not None
    assert len(path) >= 2
    assert path[0] == (0, 0)
    assert path[-1] == (1, 0)


def test_find_path_around_obstacle():
    """A* should route around unwalkable tiles."""
    walkable = {(x, y) for x in range(5) for y in range(5)}
    # Create vertical wall at x=2 (except top row)
    obstacle = {(2, y) for y in range(1, 5)}
    walkable -= obstacle

    path = find_path((0, 2), (4, 2), walkable)
    assert path is not None
    assert path[-1] == (4, 2)
    # Path should not cross the obstacle
    for tile in path:
        assert tile not in obstacle


def test_find_path_same_start_end():
    """Start == end returns single-tile path."""
    walkable = {(5, 5)}
    path = find_path((5, 5), (5, 5), walkable)
    assert path == [(5, 5)]


def test_find_path_impossible():
    """Unreachable destination returns None."""
    walkable = {(0, 0), (1, 0)}  # (5, 5) not in walkable
    path = find_path((0, 0), (5, 5), walkable)
    assert path is None


def test_find_path_long_corridor():
    """Straight corridor path is optimal length."""
    walkable = {(x, 5) for x in range(20)}
    path = find_path((0, 5), (19, 5), walkable)
    assert path is not None
    assert len(path) == 20  # 0..19 inclusive


def test_get_walkable_tiles_returns_set():
    tiles = get_walkable_tiles()
    assert isinstance(tiles, set)
    assert len(tiles) > 100  # Should cover multiple districts


def test_locations_coverage():
    """All locations in map_data should contribute walkable tiles."""
    from app.agent.map_data import LOCATIONS
    assert len(LOCATIONS) >= 4
    tiles = get_walkable_tiles()
    # Spot-check: tavern entrance area should be walkable
    assert (72, 14) in tiles


def test_find_path_heuristic_optimality():
    """A* with Manhattan heuristic finds path in bounded steps."""
    walkable = {(x, y) for x in range(20) for y in range(20)}
    path = find_path((0, 0), (19, 19), walkable)
    assert path is not None
    # Manhattan distance = 38, path length should be 39 (optimal)
    assert len(path) == 39


def test_collision_tiles_excluded():
    """Tiles blocked in the collision layer should not be walkable."""
    from app.agent.pathfinder import _load_collision_tiles, get_walkable_tiles, reset_walkable_cache
    reset_walkable_cache()
    blocked = _load_collision_tiles()
    if not blocked:
        pytest.skip("Tilemap not available in test environment")
    walkable = get_walkable_tiles()
    # At least some blocked tiles should be excluded
    excluded = blocked - walkable
    # Some blocked tiles may be force-included (entrances), but most should be excluded
    assert len(excluded) > len(blocked) * 0.9, f"Only {len(excluded)}/{len(blocked)} blocked tiles excluded"
    reset_walkable_cache()


def test_location_entrances_always_walkable():
    """Location entrances and centers must be walkable even if on collision tiles."""
    from app.agent.pathfinder import get_walkable_tiles, reset_walkable_cache, _get_forced_walkable
    reset_walkable_cache()
    walkable = get_walkable_tiles()
    forced = _get_forced_walkable()
    for tile in forced:
        assert tile in walkable, f"Forced-walkable tile {tile} not in walkable set"
    reset_walkable_cache()


def test_walkable_count_with_collisions():
    """Walkable set should be smaller than the full rectangular area when collisions loaded."""
    from app.agent.pathfinder import get_walkable_tiles, reset_walkable_cache, _load_collision_tiles
    reset_walkable_cache()
    blocked = _load_collision_tiles()
    if not blocked:
        pytest.skip("Tilemap not available in test environment")
    walkable = get_walkable_tiles()
    from app.world_geometry import WALKABLE_X_RANGE, WALKABLE_Y_RANGE
    full_rect = len(WALKABLE_X_RANGE) * len(WALKABLE_Y_RANGE)
    assert len(walkable) < full_rect, f"Walkable {len(walkable)} should be less than {full_rect}"
    assert len(walkable) > full_rect * 0.5, f"Walkable {len(walkable)} too small, expected > {full_rect * 0.5}"
    reset_walkable_cache()


def test_expanded_districts_are_in_walkable_world():
    """The east and south additions must participate in NPC pathfinding."""
    from app.agent.pathfinder import get_walkable_tiles, reset_walkable_cache
    reset_walkable_cache()
    walkable = get_walkable_tiles()
    assert (150, 56) in walkable
    assert (74, 104) in walkable
    reset_walkable_cache()


def test_tilemap_bundled_under_backend_tree():
    """The tilemap must ship inside the backend package.

    The container image is built from the backend/ tree only (Dockerfile COPY . .);
    if the tilemap resolves to the frontend monorepo path it is absent at runtime
    and collisions silently become empty (residents walk through walls).
    """
    from pathlib import Path
    from app.agent.pathfinder import _resolve_tilemap_path
    backend_root = Path(__file__).resolve().parents[1]  # backend/
    resolved = _resolve_tilemap_path()
    assert resolved.exists(), f"resolved tilemap {resolved} does not exist"
    assert backend_root in resolved.parents, (
        f"tilemap {resolved} is not under backend/ — it won't be in the image"
    )


def test_bundled_tilemap_matches_frontend_source():
    """Backend-bundled tilemap must stay byte-identical to the frontend source."""
    from pathlib import Path
    backend_tm = (
        Path(__file__).resolve().parents[1]
        / "app" / "assets" / "village" / "tilemap" / "tilemap.json"
    )
    frontend_tm = (
        Path(__file__).resolve().parents[2]
        / "frontend" / "public" / "assets" / "village" / "tilemap" / "tilemap.json"
    )
    if not frontend_tm.exists():
        pytest.skip("frontend source not present (backend-only deploy tree)")
    assert backend_tm.exists(), "backend tilemap copy missing — run `npm run map:expand`"
    assert backend_tm.read_bytes() == frontend_tm.read_bytes(), (
        "backend/frontend tilemap drifted — run `npm run map:expand` to resync"
    )


def test_reachable_tiles_exclude_disconnected_islands():
    """get_reachable_tiles() must drop walkable-but-unreachable island pockets.

    get_walkable_tiles() force-includes entrances/centers and leaves ~800 tiles
    on disconnected islands. Placing a resident on one strands them (A* to any
    building returns None). get_reachable_tiles() is the hub-connected subset.
    """
    from app.agent.pathfinder import (
        get_walkable_tiles, get_reachable_tiles, find_path, reset_walkable_cache,
    )
    from app.agent.map_data import LOCATIONS
    reset_walkable_cache()
    walkable = get_walkable_tiles()
    if (55, 54) not in walkable:
        pytest.skip("Tilemap not available in test environment")

    reachable = get_reachable_tiles()
    hub = tuple(LOCATIONS["central_plaza"]["center"])

    # Reachable is a strict subset of walkable (islands removed).
    assert reachable <= walkable
    assert len(reachable) < len(walkable)

    # (55, 54) is central_plaza's first placement slot: walkable but islanded.
    assert (55, 54) in walkable
    assert find_path(hub, (55, 54), walkable) is None  # genuinely unreachable
    assert (55, 54) not in reachable

    # Every reachable tile is truly pathable from the hub (sample to bound cost).
    for tile in list(reachable)[:200]:
        assert find_path(hub, tile, walkable) is not None
    reset_walkable_cache()
