import json
from pathlib import Path

from app.agent.map_data import LOCATIONS, get_location_id_at, get_valid_target_tile
from app.agent.pathfinder import _resolve_tilemap_path, get_reachable_tiles
from app.services.caravan_route import (
    _market_centerline_tiles,
    _on_market_road,
    _physical_open_tiles,
    build_caravan_route,
)

TILEMAP_PATH = (
    Path(__file__).resolve().parents[2]
    / "frontend" / "public" / "assets" / "village" / "tilemap" / "tilemap.json"
)


def test_caravan_route_waypoints_stay_in_expected_zones():
    route = build_caravan_route()

    town_x1, town_y1, town_x2, town_y2 = LOCATIONS["town_entrance"]["bounds"]
    hall_x1, hall_y1, hall_x2, hall_y2 = LOCATIONS["market_hall"]["bounds"]
    town_center = get_valid_target_tile("town_entrance")
    hall_parking = LOCATIONS["market_hall"]["caravan_parking"]

    assert town_x1 <= route.entrance_staging[0] <= town_x2
    assert town_y1 <= route.entrance_staging[1] <= town_y2
    assert route.entrance_staging == town_center

    assert town_x1 <= route.outside_staging[0] <= town_x2
    assert route.outside_staging[1] > town_y2
    assert route.outside_staging[0] == town_center[0]

    assert hall_x1 <= route.market_hall_parking[0] <= hall_x2
    assert hall_y1 <= route.market_hall_parking[1] <= hall_y2
    assert route.market_hall_parking == hall_parking
    assert route.outside_staging == (102, 127)


def test_caravan_route_anchor_path_is_deterministic_and_contiguous():
    route_a = build_caravan_route()
    route_b = build_caravan_route()

    assert route_a.full_path == route_b.full_path
    assert route_a.approach_path[0] == route_a.outside_staging
    assert route_a.approach_path[-1] == route_a.entrance_staging
    assert route_a.market_hall_path[0] == route_a.entrance_staging
    assert route_a.market_hall_path[-1] == route_a.market_hall_parking
    assert route_a.full_path[0] == route_a.outside_staging
    assert route_a.full_path[-1] == route_a.market_hall_parking
    assert route_a.anchor_reachable is True

    for current, nxt in zip(route_a.full_path, route_a.full_path[1:]):
        assert abs(current[0] - nxt[0]) + abs(current[1] - nxt[1]) == 1


def test_caravan_route_uses_physical_open_tiles_only():
    route = build_caravan_route()
    physical_open = _physical_open_tiles()

    for tile in route.full_path:
        assert tile in physical_open
    assert route.outside_staging in physical_open
    assert route.entrance_staging in physical_open
    assert route.market_hall_parking in physical_open


def test_caravan_route_avoids_buildings_except_market_hall_loading_aisle():
    route = build_caravan_route()

    for tile in route.full_path:
        loc_id = get_location_id_at(*tile)
        if loc_id is None:
            continue
        assert LOCATIONS[loc_id]["type"] == "outdoor" or loc_id == "market_hall"


def test_caravan_route_stays_on_visible_market_road_centerline():
    route = build_caravan_route()
    centerline = _market_centerline_tiles()

    assert all(_on_market_road(tile) for tile in route.full_path)
    assert set(route.full_path) <= centerline
    assert len(route.turns) == 1
    assert [turn.tile for turn in route.turns] == [(102, 94)]


def test_caravan_route_matches_visible_pavement_and_never_crosses_structure_art():
    route = build_caravan_route()
    tilemap = json.loads(_resolve_tilemap_path().read_text())
    width = tilemap["width"]
    layers = {
        layer["name"]: layer["data"]
        for layer in tilemap["layers"]
        if isinstance(layer.get("data"), list)
    }

    for x, y in route.full_path:
        index = y * width + x
        on_exterior = layers["Exterior Ground"][index] != 0
        in_hall = layers["Interior Ground"][index] != 0
        assert on_exterior != in_hall, (x, y)
        assert layers["Wall"][index] == 0, (x, y)
        assert layers["Interior Furniture L1"][index] == 0, (x, y)
        assert layers["Interior Furniture L2 "][index] == 0, (x, y)
        assert layers["Exterior Decoration L1"][index] == 0, (x, y)
        assert layers["Exterior Decoration L2"][index] == 0, (x, y)


def test_caravan_route_has_no_tree_or_foreground_tiles_on_the_anchor_path():
    route = build_caravan_route()
    tilemap = json.loads(TILEMAP_PATH.read_text())
    width = tilemap["width"]
    layers = {
        layer["name"]: layer["data"]
        for layer in tilemap["layers"]
        if layer["type"] == "tilelayer"
    }

    for x, y in route.full_path:
        idx = y * width + x
        assert layers["Exterior Decoration L1"][idx] == 0
        assert layers["Exterior Decoration L2"][idx] == 0
        assert layers["Foreground L1"][idx] == 0
        assert layers["Foreground L2"][idx] == 0


def test_caravan_edge_exit_does_not_pollute_resident_reachability_cache():
    before = set(get_reachable_tiles())
    assert (102, 127) not in before

    build_caravan_route()

    assert set(get_reachable_tiles()) == before
    assert (102, 127) not in get_reachable_tiles()


def test_caravan_route_directions_and_turns_match_the_path():
    route = build_caravan_route()

    assert len(route.step_directions) == len(route.full_path) - 1
    assert all(direction in {"north", "south", "east", "west"} for direction in route.step_directions)

    expected_turns = 0
    for prev_dir, next_dir in zip(route.step_directions, route.step_directions[1:]):
        if prev_dir != next_dir:
            expected_turns += 1
    assert len(route.turns) == expected_turns

    for turn in route.turns:
        assert turn.from_direction != turn.to_direction
        assert turn.tile in route.full_path


def test_caravan_clearance_report_is_auditable():
    route = build_caravan_route()
    clearance = route.clearance

    assert clearance.anchor_footprint == "1x1"
    assert clearance.visual_overhang_px == 64
    assert clearance.overhang_radius_tiles == 2
    assert clearance.min_blocked_chebyshev_distance is None or clearance.min_blocked_chebyshev_distance >= 1
    assert set(clearance.risky_tiles) <= set(route.full_path)
    assert isinstance(clearance.two_tile_width_supported, bool)
    assert clearance.two_tile_width_supported is True
    # Only the two final off-map staging anchors approach the world boundary;
    # the entire in-town road has the full visual envelope.
    assert set(clearance.risky_tiles) == {(102, 126), (102, 127)}

    if not clearance.two_tile_width_supported:
        assert clearance.two_tile_width_blockers
        assert any("1x1 anchor" in risk or "visual overhang" in risk for risk in route.risks)

    payload = route.to_dict()
    assert payload["outside_staging"] == list(route.outside_staging)
    assert payload["market_hall_parking"] == list(route.market_hall_parking)
    assert payload["plaza_parking"] == list(route.market_hall_parking)
