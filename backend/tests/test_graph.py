"""C2 relationship graph: aggregation, mutual, min_importance, player-edge privacy."""

import pytest

from app.models.user import User
from app.models.resident import Resident
from app.models.memory import Memory


@pytest.fixture(autouse=True)
def _clear_graph_cache():
    from app.routers import graph
    graph._invalidate()
    yield
    graph._invalidate()


async def _resident(db, slug):
    r = Resident(slug=slug, name=slug, creator_id="system",
                 district="central_plaza", status="idle", tile_x=1, tile_y=1)
    db.add(r)
    await db.commit()
    return r


async def _rel(db, resident_id, related_resident_id=None, related_user_id=None, imp=0.5, content="朋友"):
    db.add(Memory(resident_id=resident_id, type="relationship", content=content,
                  importance=imp, source="relationship",
                  related_resident_id=related_resident_id, related_user_id=related_user_id))
    await db.commit()


@pytest.mark.anyio
async def test_cold_start_empty_edges(client, db_session):
    await _resident(db_session, "a")
    resp = await client.get("/graph/relationships")
    assert resp.status_code == 200
    assert resp.json()["edges"] == []


@pytest.mark.anyio
async def test_mutual_edge_deduped(client, db_session):
    a = await _resident(db_session, "a")
    b = await _resident(db_session, "b")
    await _rel(db_session, a.id, related_resident_id=b.id, imp=0.6, content="好友")
    await _rel(db_session, b.id, related_resident_id=a.id, imp=0.4)

    edges = (await client.get("/graph/relationships")).json()["edges"]
    assert len(edges) == 1 and edges[0]["mutual"] is True
    assert edges[0]["strength"] == 0.6  # max of the two


@pytest.mark.anyio
async def test_min_importance_filter(client, db_session):
    a = await _resident(db_session, "a")
    b = await _resident(db_session, "b")
    await _rel(db_session, a.id, related_resident_id=b.id, imp=0.2)
    edges = (await client.get("/graph/relationships?min_importance=0.3")).json()["edges"]
    assert edges == []


@pytest.mark.anyio
async def test_player_edge_only_for_self(client, db_session):
    from app.services.auth_service import create_token
    a = await _resident(db_session, "a")
    user = User(name="u", email="graph@test.com")
    db_session.add(user)
    await db_session.commit()
    await _rel(db_session, a.id, related_user_id=user.id, imp=0.7, content="我的玩家朋友")

    # Anonymous: no player edge.
    anon = (await client.get("/graph/relationships")).json()
    assert not any(e["a"] == "__you__" for e in anon["edges"])

    # Authenticated as that user: own edge visible.
    me = (await client.get("/graph/relationships",
                           headers={"Authorization": f"Bearer {create_token(user.id)}"})).json()
    assert any(e["a"] == "__you__" and e["b"] == "a" for e in me["edges"])
