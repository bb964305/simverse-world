import hashlib

import pytest

from app.models.user import User
from app.services.auth_service import create_token


@pytest.mark.anyio
async def test_wallet_user_uploads_and_downloads_private_anchor(client, db_session, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "web3_content_dir", str(tmp_path))
    user = User(
        id="wallet-content-user",
        name="Wallet User",
        email="wallet-content@identity.simverse.world",
        hashed_password=None,
        wallet_address="0x1234567890123456789012345678901234567890",
    )
    db_session.add(user)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_token(user.id)}"}
    payload = b'{"memory":"the city remembers"}'

    uploaded = await client.post(
        "/web3/content",
        headers=headers,
        files={"file": ("memory.json", payload, "application/json")},
    )
    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["content_hash"] == f"0x{hashlib.sha256(payload).hexdigest()}"
    assert body["content_uri"].endswith(f'/web3/content/{body["content_id"]}')

    downloaded = await client.get(f'/web3/content/{body["content_id"]}', headers=headers)
    assert downloaded.status_code == 200
    assert downloaded.content == payload


@pytest.mark.anyio
async def test_web2_identity_cannot_use_web3_content_store(client):
    registered = await client.post(
        "/auth/register",
        json={"name": "Legacy", "email": "legacy@example.com", "password": "password123"},
    )
    token = registered.json()["access_token"]
    response = await client.post(
        "/web3/content",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("memory.txt", b"legacy", "text/plain")},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Wallet identity required"
