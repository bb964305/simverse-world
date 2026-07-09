"""S4 notification center: notify (persist + WS push) and the API."""

from unittest.mock import AsyncMock, patch

import pytest

from app.models.user import User
from app.models.notification import Notification


@pytest.mark.anyio
async def test_notify_persists_row(db_session):
    from app.services import notification_service as svc

    with patch.object(svc.manager, "is_online", AsyncMock(return_value=False)):
        n = await svc.notify(db_session, "u1", "system", "标题", "正文", {"k": 1})

    assert n.id and n.read_at is None
    got = (await db_session.execute(Notification.__table__.select())).first()
    assert got.user_id == "u1" and got.kind == "system" and got.title == "标题"


@pytest.mark.anyio
async def test_notify_pushes_ws_when_online(db_session):
    from app.services import notification_service as svc

    with patch.object(svc.manager, "is_online", AsyncMock(return_value=True)), \
         patch.object(svc.manager, "send", new_callable=AsyncMock) as send:
        await svc.notify(db_session, "u1", "achievement", "解锁", "首次对话")

    send.assert_awaited_once()
    uid, msg = send.call_args.args
    assert uid == "u1"
    assert msg["type"] == "notification" and msg["kind"] == "achievement"


@pytest.mark.anyio
async def test_notify_swallows_ws_failure(db_session):
    from app.services import notification_service as svc

    with patch.object(svc.manager, "is_online", AsyncMock(return_value=True)), \
         patch.object(svc.manager, "send", AsyncMock(side_effect=RuntimeError("gone"))):
        n = await svc.notify(db_session, "u1", "system", "t")  # must not raise

    assert n.id  # still persisted


@pytest.mark.anyio
async def test_notifications_api_list_and_mark_read(client, db_session):
    from app.services import notification_service as svc
    from app.services.auth_service import create_token

    user = User(name="n", email="notif@test.com")
    db_session.add(user)
    await db_session.commit()

    with patch.object(svc.manager, "is_online", AsyncMock(return_value=False)):
        n1 = await svc.notify(db_session, user.id, "system", "第一条")
        await svc.notify(db_session, user.id, "achievement", "第二条")

    headers = {"Authorization": f"Bearer {create_token(user.id)}"}

    resp = await client.get("/notifications", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["notifications"]) == 2
    assert data["unread_count"] == 2

    # Mark the first one read.
    r2 = await client.post("/notifications/read", json={"ids": [n1.id]}, headers=headers)
    assert r2.status_code == 200
    assert r2.json()["unread_count"] == 1

    # unread_only now returns just the still-unread one.
    r3 = await client.get("/notifications?unread_only=true", headers=headers)
    assert r3.json()["unread_count"] == 1
    assert len(r3.json()["notifications"]) == 1


@pytest.mark.anyio
async def test_notifications_api_requires_auth(client):
    resp = await client.get("/notifications")
    assert resp.status_code == 401
