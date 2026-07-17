import json
from pathlib import Path

from app.agent.map_data import LOCATIONS
from app.world_geometry import MAP_HEIGHT_TILES, MAP_WIDTH_TILES


TILEMAP_PATH = (
    Path(__file__).resolve().parents[2]
    / "frontend" / "public" / "assets" / "village" / "tilemap" / "tilemap.json"
)
MAZE_PATH = TILEMAP_PATH.parents[1] / "maze.json"


def test_tilemap_layers_match_world_geometry():
    tilemap = json.loads(TILEMAP_PATH.read_text())

    assert (tilemap["width"], tilemap["height"]) == (MAP_WIDTH_TILES, MAP_HEIGHT_TILES)
    for layer in tilemap["layers"]:
        if layer["type"] != "tilelayer":
            continue
        assert (layer["width"], layer["height"]) == (MAP_WIDTH_TILES, MAP_HEIGHT_TILES)
        assert len(layer["data"]) == MAP_WIDTH_TILES * MAP_HEIGHT_TILES

    maze = json.loads(MAZE_PATH.read_text())
    assert maze["size"] == [MAP_HEIGHT_TILES, MAP_WIDTH_TILES]


def test_all_static_locations_fit_inside_map():
    for location_id, location in LOCATIONS.items():
        x1, y1, x2, y2 = location["bounds"]
        assert 0 <= x1 <= x2 < MAP_WIDTH_TILES, location_id
        assert 0 <= y1 <= y2 < MAP_HEIGHT_TILES, location_id
