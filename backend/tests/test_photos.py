"""E10 photo log: resident memory written + mood-based quip."""

import pytest
from sqlalchemy import select

from app.models.user import User
from app.models.resident import Resident
from app.models.memory import Memory
from app.services.auth_service import create_token


async def _user(db, email):
    u = User(name="小明", email=email)
    db.add(u)
    await db.commit()
    return u


async def _resident(db, slug, mood=None):
    r = Resident(slug=slug, name="克劳斯", creator_id="system", district="cafe", status="idle",
                 tile_x=1, tile_y=1, mood_json=mood)
    db.add(r)
    await db.commit()
    return r


@pytest.mark.anyio
async def test_photo_log_writes_memory_and_quip(client, db_session):
    user = await _user(db_session, "photo@t.com")
    await _resident(db_session, "klaus", mood={"label": "excited", "valence": 0.8, "arousal": 0.8, "updated_at": "x"})

    resp = await client.post("/photos/log", json={"resident_slug": "klaus", "media_url": "/media/x.png"},
                             headers={"Authorization": f"Bearer {create_token(user.id)}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["resident_slug"] == "klaus" and "留着" in body["quip"]  # excited quip

    mems = (await db_session.execute(select(Memory).where(Memory.source == "photo"))).scalars().all()
    assert len(mems) == 1
    assert mems[0].importance == 0.5 and mems[0].related_user_id == user.id
    assert mems[0].metadata_json["media_url"] == "/media/x.png"


@pytest.mark.anyio
async def test_photo_log_default_quip_when_no_mood(client, db_session):
    user = await _user(db_session, "p2@t.com")
    await _resident(db_session, "klaus")
    resp = await client.post("/photos/log", json={"resident_slug": "klaus"},
                             headers={"Authorization": f"Bearer {create_token(user.id)}"})
    assert resp.status_code == 200 and resp.json()["quip"]


@pytest.mark.anyio
async def test_photo_log_unknown_resident(client, db_session):
    user = await _user(db_session, "p3@t.com")
    resp = await client.post("/photos/log", json={"resident_slug": "nobody"},
                             headers={"Authorization": f"Bearer {create_token(user.id)}"})
    assert resp.status_code == 404
