"""B3 home decor: bounds/purchase/cap validation, owner 403, roundtrip,
broadcast, lazy home claim, NPC notice easter egg."""

from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.memory import Memory
from app.models.resident import Resident
from app.models.shop import Item, Purchase
from app.models.user import User

# house_a bounds in map_data: (65, 14, 69, 26) → offsets 0..4 / 0..12
HOUSE_A = "house_a"


async def _user(db, email):
    u = User(name="decor", email=email, soul_coin_balance=500)
    db.add(u)
    await db.commit()
    return u


async def _player(db, user, slug, home=HOUSE_A):
    r = Resident(
        slug=slug, name=f"玩家{slug}", resident_type="player",
        creator_id=user.id, home_location_id=home,
    )
    db.add(r)
    await db.commit()
    return r


async def _grant(db, user_id, code="decor_lamp", qty=1, name="落地灯"):
    """Catalog decor item + a purchase covering ``qty`` placements."""
    existing = (await db.execute(select(Item).where(Item.code == code))).scalar_one_or_none()
    if not existing:
        db.add(Item(code=code, kind="decor", name=name, price_sc=10))
    db.add(Purchase(user_id=user_id, item_code=code, qty=qty, total_sc=10 * qty))
    await db.commit()


def _auth(user_id):
    from app.services.auth_service import create_token
    return {"Authorization": f"Bearer {create_token(user_id)}"}


@pytest.mark.anyio
async def test_put_out_of_bounds_400(client, db_session):
    user = await _user(db_session, "oob@b3.com")
    await _player(db_session, user, "p-oob")
    await _grant(db_session, user.id)

    resp = await client.put(
        "/residents/p-oob/home/decor",
        json={"items": [{"item_code": "decor_lamp", "x": 5, "y": 0, "rot": 0}]},
        headers=_auth(user.id),
    )
    assert resp.status_code == 400
    assert "超出住房范围" in resp.json()["detail"]


@pytest.mark.anyio
async def test_put_unpurchased_400(client, db_session):
    user = await _user(db_session, "nobuy@b3.com")
    await _player(db_session, user, "p-nobuy")

    # Never purchased at all.
    resp = await client.put(
        "/residents/p-nobuy/home/decor",
        json={"items": [{"item_code": "decor_lamp", "x": 0, "y": 0, "rot": 0}]},
        headers=_auth(user.id),
    )
    assert resp.status_code == 400

    # Bought 1, tries to place 2.
    await _grant(db_session, user.id, qty=1)
    resp = await client.put(
        "/residents/p-nobuy/home/decor",
        json={"items": [
            {"item_code": "decor_lamp", "x": 0, "y": 0, "rot": 0},
            {"item_code": "decor_lamp", "x": 1, "y": 1, "rot": 0},
        ]},
        headers=_auth(user.id),
    )
    assert resp.status_code == 400
    assert "数量不足" in resp.json()["detail"]


@pytest.mark.anyio
async def test_put_not_owner_403(client, db_session):
    owner = await _user(db_session, "own@b3.com")
    stranger = await _user(db_session, "str@b3.com")
    await _player(db_session, owner, "p-own")
    await _grant(db_session, stranger.id)

    resp = await client.put(
        "/residents/p-own/home/decor",
        json={"items": []},
        headers=_auth(stranger.id),
    )
    assert resp.status_code == 403

    # A non-player resident can't be decorated even by its creator.
    npc = Resident(slug="npc-own", name="NPC", resident_type="npc",
                   creator_id=owner.id, home_location_id=HOUSE_A)
    db_session.add(npc)
    await db_session.commit()
    resp = await client.put(
        "/residents/npc-own/home/decor", json={"items": []}, headers=_auth(owner.id),
    )
    assert resp.status_code == 403


@pytest.mark.anyio
async def test_put_get_roundtrip(client, db_session):
    user = await _user(db_session, "rt@b3.com")
    await _player(db_session, user, "p-rt")
    await _grant(db_session, user.id, qty=2)

    items = [
        {"item_code": "decor_lamp", "x": 0, "y": 0, "rot": 0},
        {"item_code": "decor_lamp", "x": 2, "y": 5, "rot": 90},
    ]
    resp = await client.put(
        "/residents/p-rt/home/decor", json={"items": items}, headers=_auth(user.id),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["home_location_id"] == HOUSE_A
    assert body["bounds"] == [65, 14, 69, 26]
    assert body["items"] == items

    # GET is public — no auth header.
    got = await client.get("/residents/p-rt/home/decor")
    assert got.status_code == 200
    assert got.json()["items"] == items

    # Persisted on the row (full replace).
    r = (await db_session.execute(
        select(Resident).where(Resident.slug == "p-rt")
    )).scalar_one()
    await db_session.refresh(r)
    assert r.home_decor_json == items


@pytest.mark.anyio
async def test_put_broadcasts_decor_updated(client, db_session):
    from app.services import home_decor_service as hds

    user = await _user(db_session, "bc@b3.com")
    await _player(db_session, user, "p-bc")
    await _grant(db_session, user.id)

    sent: list[dict] = []

    class _FakeManager:
        async def broadcast(self, data, exclude=None):
            sent.append(data)

    with patch.object(hds, "manager", _FakeManager()):
        resp = await client.put(
            "/residents/p-bc/home/decor",
            json={"items": [{"item_code": "decor_lamp", "x": 1, "y": 1, "rot": 0}]},
            headers=_auth(user.id),
        )
    assert resp.status_code == 200
    assert len(sent) == 1
    assert sent[0]["type"] == "decor_updated"
    assert sent[0]["resident_slug"] == "p-bc"
    assert sent[0]["decor"][0]["item_code"] == "decor_lamp"


@pytest.mark.anyio
async def test_put_cap_12_rejected(client, db_session):
    user = await _user(db_session, "cap@b3.com")
    await _player(db_session, user, "p-cap")
    await _grant(db_session, user.id, qty=13)

    items = [{"item_code": "decor_lamp", "x": 0, "y": i % 13, "rot": 0} for i in range(13)]
    resp = await client.put(
        "/residents/p-cap/home/decor", json={"items": items}, headers=_auth(user.id),
    )
    assert resp.status_code == 400
    assert "12" in resp.json()["detail"]


@pytest.mark.anyio
async def test_put_claims_home_when_missing(client, db_session):
    """Onboarding gives players no home (assign_housing=False) — first decor
    write lazily claims a free slot, counting players in occupancy."""
    user = await _user(db_session, "claim@b3.com")
    r = Resident(slug="p-claim", name="无房玩家", resident_type="player",
                 creator_id=user.id, home_location_id=None)
    db_session.add(r)
    await db_session.commit()

    resp = await client.put(
        "/residents/p-claim/home/decor", json={"items": []}, headers=_auth(user.id),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["home_location_id"] is not None
    assert isinstance(body["bounds"], list) and len(body["bounds"]) == 4

    await db_session.refresh(r)
    assert r.home_location_id == body["home_location_id"]


@pytest.fixture
def decor_env(db_engine):
    from app.services import home_decor_service as hds
    hds._reset_for_tests()
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    with patch.object(hds, "async_session", factory):
        yield hds
    hds._reset_for_tests()


@pytest.mark.anyio
async def test_notice_decor_changes_writes_memory_once(db_session, decor_env):
    hds = decor_env
    owner = Resident(slug="p-home", name="小明", resident_type="player",
                     creator_id="u-any", home_location_id=HOUSE_A,
                     home_decor_json=[{"item_code": "decor_lamp", "x": 0, "y": 0, "rot": 0}])
    db_session.add(owner)
    db_session.add(Item(code="decor_plant", kind="decor", name="盆栽", price_sc=40))
    await db_session.commit()

    inside = (66, 15)  # inside house_a
    # Outdoors → nothing.
    assert await hds.notice_decor_changes("obs1", 75, 56) == 0
    # First sighting only primes the cache.
    assert await hds.notice_decor_changes("obs1", *inside) == 0
    # Unchanged decor → still nothing.
    assert await hds.notice_decor_changes("obs1", *inside) == 0

    owner.home_decor_json = [
        {"item_code": "decor_lamp", "x": 0, "y": 0, "rot": 0},
        {"item_code": "decor_plant", "x": 1, "y": 2, "rot": 0},
    ]
    await db_session.commit()

    assert await hds.notice_decor_changes("obs1", *inside) == 1
    # Same hash again → no duplicate memory.
    assert await hds.notice_decor_changes("obs1", *inside) == 0

    mems = (await db_session.execute(
        select(Memory).where(Memory.resident_id == "obs1", Memory.source == "decor")
    )).scalars().all()
    assert len(mems) == 1
    assert mems[0].importance == 0.3
    assert mems[0].related_resident_id == owner.id
    assert "小明" in mems[0].content and "盆栽" in mems[0].content
