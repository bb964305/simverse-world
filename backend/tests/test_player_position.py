"""Test PUT /residents/player/position endpoint."""
import pytest
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.schemas.resident import PlayerPositionUpdate


@pytest.mark.asyncio
async def test_update_player_position_requires_auth():
    """Unauthenticated requests should return 401."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.put("/residents/player/position", json={"tile_x": 10, "tile_y": 20})
    assert resp.status_code == 401


def test_player_position_accepts_expanded_map_edge():
    position = PlayerPositionUpdate(tile_x=179, tile_y=127)
    assert (position.tile_x, position.tile_y) == (179, 127)


@pytest.mark.parametrize("tile_x,tile_y", [(180, 127), (179, 128)])
def test_player_position_rejects_outside_expanded_map(tile_x, tile_y):
    with pytest.raises(ValueError):
        PlayerPositionUpdate(tile_x=tile_x, tile_y=tile_y)
