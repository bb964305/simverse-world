"""C1 soul card: card (public), export (owner), import (fidelity, sensitive block, cap)."""

import pytest

from app.models.user import User
from app.models.resident import Resident
from app.services.auth_service import create_token


async def _user(db, email):
    u = User(name="u", email=email, soul_coin_balance=100)
    db.add(u)
    await db.commit()
    return u


async def _resident(db, slug, creator_id):
    r = Resident(slug=slug, name="克劳斯", creator_id=creator_id, district="cafe", status="idle",
                 tile_x=1, tile_y=1, star_rating=3, ability_md="# 能力", persona_md="# 人格",
                 soul_md="核心价值观：诚实。\n\n更多细节。", meta_json={"sbti": {"type": "OJBK", "type_name": "无所谓人"}})
    db.add(r)
    await db.commit()
    return r


@pytest.mark.anyio
async def test_card_public(client, db_session):
    u = await _user(db_session, "card@t.com")
    await _resident(db_session, "klaus", u.id)
    r = await client.get("/residents/klaus/card")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "克劳斯" and body["sbti_type"] == "OJBK" and body["star_rating"] == 3
    assert "诚实" in body["soul_excerpt"]


@pytest.mark.anyio
async def test_export_owner_only(client, db_session):
    owner = await _user(db_session, "own@t.com")
    other = await _user(db_session, "oth@t.com")
    await _resident(db_session, "klaus", owner.id)

    forbidden = await client.get("/residents/klaus/export", headers={"Authorization": f"Bearer {create_token(other.id)}"})
    assert forbidden.status_code == 403

    ok = await client.get("/residents/klaus/export", headers={"Authorization": f"Bearer {create_token(owner.id)}"})
    assert ok.status_code == 200
    data = ok.json()
    assert data["name"] == "克劳斯" and data["sbti"]["type"] == "OJBK"
    assert data["ability_md"] and data["persona_md"] and data["soul_md"]


@pytest.mark.anyio
async def test_import_roundtrip_and_cap(client, db_session):
    owner = await _user(db_session, "imp@t.com")
    payload = {
        "name": "新居民", "ability_md": "# 能力A", "persona_md": "# 人格A",
        "soul_md": "# 灵魂A", "sbti": {"type": "OJBK", "type_name": "无所谓人"},
    }
    headers = {"Authorization": f"Bearer {create_token(owner.id)}"}

    r1 = await client.post("/residents/import-card", json=payload, headers=headers)
    assert r1.status_code == 200
    # exported card of the imported resident preserves SBTI + layers.
    slug = r1.json()["slug"]
    exp = await client.get(f"/residents/{slug}/export", headers=headers)
    assert exp.json()["sbti"]["type"] == "OJBK" and exp.json()["ability_md"] == "# 能力A"

    # daily cap: 2 more ok (total 3), 4th → 429.
    for i in range(2):
        assert (await client.post("/residents/import-card", json={**payload, "name": f"r{i}"}, headers=headers)).status_code == 200
    assert (await client.post("/residents/import-card", json={**payload, "name": "over"}, headers=headers)).status_code == 429


@pytest.mark.anyio
async def test_import_sensitive_blocked(client, db_session):
    owner = await _user(db_session, "sens@t.com")
    r = await client.post("/residents/import-card", json={"name": "x", "persona_md": "fuck you", "soul_md": "x"},
                          headers={"Authorization": f"Bearer {create_token(owner.id)}"})
    assert r.status_code == 400
