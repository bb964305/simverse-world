"""Shared tile-space geometry for the town world."""

MAP_WIDTH_TILES = 180
MAP_HEIGHT_TILES = 128
TILE_SIZE = 32

# Keep residents away from the decorative outer border while allowing them to
# use both expansion districts. Collisions remove buildings and dense foliage.
WALKABLE_X_RANGE = range(14, MAP_WIDTH_TILES - 6)
WALKABLE_Y_RANGE = range(12, MAP_HEIGHT_TILES - 4)
