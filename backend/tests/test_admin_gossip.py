"""E3 admin rumor-chain routes: chain trace (incl. hop→origin resolve), recent list, 403."""

import pytest

from app.models.memory import Memory
from app.models.resident import Resident
from app.models.user import User
from app.services.auth_service import create_token


async def _resident(db, slug):
    r = Resident(slug=slug, name=f"居民{slug}", creator_id="system", district="cafe",
                 status="idle", tile_x=1, tile_y=1)
    db.add(r)
    await db.commit()
    return r


async def _seed_chain(db):
    """origin (a about c) → hop1 gossip (b) → hop2 gossip (x, distorted)."""
    a = await _resident(db, "a")
    b = await _resident(db, "b")
    c = await _resident(db, "c")
    x = await _resident(db, "x")
    origin = Memory(resident_id=a.id, type="event", content="C 昨天摔了一跤",
                    importance=0.8, source="event", related_resident_id=c.id)
    db.add(origin)
    await db.commit()
    hop1 = Memory(resident_id=b.id, type="event", content="C 昨天摔了一跤",
                  importance=0.64, source="gossip", related_resident_id=c.id,
                  metadata_json={"origin_memory_id": origin.id, "hops": 1, "distorted": False})
    hop2 = Memory(resident_id=x.id, type="event", content="C 昨天从楼上摔了下来！",
                  importance=0.5, source="gossip", related_resident_id=c.id,
                  metadata_json={"origin_memory_id": origin.id, "hops": 2, "distorted": True})
    db.add_all([hop1, hop2])
    await db.commit()
    return origin, hop1, hop2


async def _admin_headers(db):
    admin = User(name="adm", email="adm-gossip@test.com", is_admin=True, is_banned=False)
    db.add(admin)
    await db.commit()
    return {"Authorization": f"Bearer {create_token(admin.id)}"}


@pytest.mark.anyio
async def test_rumor_chain_route_traces_origin_and_hops(client, db_session):
    origin, hop1, hop2 = await _seed_chain(db_session)
    headers = await _admin_headers(db_session)

    resp = await client.get(f"/admin/gossip/chains/{origin.id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["origin_memory_id"] == origin.id
    chain = body["chain"]
    assert [c["id"] for c in chain] == [origin.id, hop1.id, hop2.id]  # sorted by hops
    assert chain[0]["hops"] == 0 and chain[0]["distorted"] is False
    assert chain[2]["hops"] == 2 and chain[2]["distorted"] is True
    assert chain[1]["resident_name"] == "居民b"
    assert chain[1]["importance"] == 0.64

    # Clicking a mid-chain hop resolves back to the same full chain.
    resp2 = await client.get(f"/admin/gossip/chains/{hop2.id}", headers=headers)
    assert resp2.status_code == 200
    assert resp2.json()["origin_memory_id"] == origin.id
    assert len(resp2.json()["chain"]) == 3

    # Unknown memory → 404.
    resp3 = await client.get("/admin/gossip/chains/nope", headers=headers)
    assert resp3.status_code == 404


@pytest.mark.anyio
async def test_recent_gossip_lists_only_gossip_memories(client, db_session):
    origin, hop1, hop2 = await _seed_chain(db_session)
    headers = await _admin_headers(db_session)

    resp = await client.get("/admin/gossip/recent", headers=headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    ids = {i["id"] for i in items}
    assert ids == {hop1.id, hop2.id}  # the plain event origin is excluded
    assert all(i["origin_memory_id"] == origin.id for i in items)
    distorted = next(i for i in items if i["id"] == hop2.id)
    assert distorted["distorted"] is True and distorted["hops"] == 2


@pytest.mark.anyio
async def test_gossip_routes_require_admin(client, db_session):
    origin, _, _ = await _seed_chain(db_session)
    user = User(name="pleb", email="pleb-gossip@test.com", is_admin=False, is_banned=False)
    db_session.add(user)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_token(user.id)}"}

    assert (await client.get("/admin/gossip/recent", headers=headers)).status_code == 403
    assert (await client.get(f"/admin/gossip/chains/{origin.id}", headers=headers)).status_code == 403
    # No token at all → 401.
    assert (await client.get("/admin/gossip/recent")).status_code == 401
