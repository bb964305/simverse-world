"""E7 time capsules: create (free/fee), delivery idempotent, sealed privacy."""

from datetime import datetime, timedelta, UTC
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.models.user import User
from app.models.resident import Resident
from app.models.memory import Memory
from app.models.time_capsule import TimeCapsule
from app.services.auth_service import create_token


async def _user(db, email, bal=100):
    u = User(name="u", email=email, soul_coin_balance=bal)
    db.add(u)
    await db.commit()
    return u


async def _carrier(db, slug="klaus"):
    r = Resident(slug=slug, name="克劳斯", creator_id="system", district="cafe", status="idle", tile_x=1, tile_y=1)
    db.add(r)
    await db.commit()
    return r


def _future(days):
    return (datetime.now(UTC).date() + timedelta(days=days))


@pytest.mark.anyio
async def test_create_first_free_then_fee(db_session):
    from app.services.capsule_service import create_capsule
    user = await _user(db_session, "cap@t.com", bal=100)
    await _carrier(db_session)

    c1 = await create_capsule(db_session, user.id, "klaus", _future(30), "给未来的我")
    assert c1.status == "sealed"
    await db_session.refresh(user)
    assert user.soul_coin_balance == 100  # first free

    await create_capsule(db_session, user.id, "klaus", _future(60), "第二封")
    await db_session.refresh(user)
    assert user.soul_coin_balance == 90  # 10 fee

    # carrier got a keeping memory.
    mems = (await db_session.execute(select(Memory).where(Memory.source == "capsule"))).scalars().all()
    assert len(mems) == 2 and all(m.importance == 0.6 for m in mems)


@pytest.mark.anyio
async def test_create_rejects_bad_date(db_session):
    from app.services.capsule_service import create_capsule, CapsuleError
    user = await _user(db_session, "bad@t.com")
    await _carrier(db_session)
    with pytest.raises(CapsuleError):
        await create_capsule(db_session, user.id, "klaus", _future(1), "太快了")  # < 3 days


@pytest.mark.anyio
async def test_delivery_idempotent(db_session):
    from app.services.capsule_service import deliver_due_capsules
    user = await _user(db_session, "del@t.com")
    db_session.add(TimeCapsule(user_id=user.id, carrier_resident_slug="klaus",
                               deliver_on=datetime.now(UTC).date() - timedelta(days=1),
                               content="到期的信", status="sealed"))
    await db_session.commit()

    with patch("app.services.notification_service.manager.is_online", AsyncMock(return_value=False)):
        n1 = await deliver_due_capsules(db_session)
        n2 = await deliver_due_capsules(db_session)  # idempotent

    assert n1 == 1 and n2 == 0
    c = (await db_session.execute(select(TimeCapsule))).scalar_one()
    assert c.status == "delivered" and c.resident_note and c.delivered_at


@pytest.mark.anyio
async def test_sealed_content_privacy(client, db_session):
    owner = await _user(db_session, "own@t.com")
    other = await _user(db_session, "oth@t.com")
    await _carrier(db_session)
    cap = TimeCapsule(user_id=owner.id, carrier_resident_slug="klaus",
                      deliver_on=_future(30), content="秘密内容", status="sealed")
    db_session.add(cap)
    await db_session.commit()

    # Other user cannot read a sealed capsule.
    forbidden = await client.get(f"/capsules/{cap.id}", headers={"Authorization": f"Bearer {create_token(other.id)}"})
    assert forbidden.status_code == 403

    # Owner sees content.
    ok = await client.get(f"/capsules/{cap.id}", headers={"Authorization": f"Bearer {create_token(owner.id)}"})
    assert ok.status_code == 200 and ok.json()["content"] == "秘密内容"
