import hashlib

import pytest

from app.models.user import User
from app.models.resident import Resident
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
async def test_upload_uses_configured_public_api_prefix(client, db_session, tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "web3_content_dir", str(tmp_path))
    monkeypatch.setattr(settings, "web3_public_api_base_url", "https://simverse.space/api/")
    user = User(
        id="wallet-public-uri-user",
        name="Wallet Public URI User",
        email="wallet-public-uri@identity.simverse.world",
        hashed_password=None,
        wallet_address="0x2234567890123456789012345678901234567890",
    )
    db_session.add(user)
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_token(user.id)}"}

    uploaded = await client.post(
        "/web3/content",
        headers=headers,
        files={"file": ("training.json", b'{"skill":"builder"}', "application/json")},
    )

    assert uploaded.status_code == 200
    assert uploaded.json()["content_uri"] == (
        f'https://simverse.space/api/web3/content/{uploaded.json()["content_id"]}'
    )


@pytest.mark.anyio
async def test_passport_metadata_is_public_but_private_anchors_stay_private(
    client, db_session, tmp_path, monkeypatch
):
    from app.config import settings

    monkeypatch.setattr(settings, "web3_content_dir", str(tmp_path))
    user = User(
        id="wallet-passport-user", name="Passport User",
        email="wallet-passport@identity.simverse.world", hashed_password=None,
        wallet_address="0x3234567890123456789012345678901234567890",
    )
    resident = Resident(
        id="resident-public-passport", slug="p-public-passport", name="Nova",
        creator_id=user.id, resident_type="player", sprite_key="埃迪",
    )
    db_session.add_all([user, resident])
    await db_session.commit()
    headers = {"Authorization": f"Bearer {create_token(user.id)}"}

    created = await client.post(
        f"/web3/content/passport-metadata/{resident.id}", headers=headers
    )
    assert created.status_code == 200
    body = created.json()
    assert f"/web3/content/public/{body['content_id']}" in body["content_uri"]

    public = await client.get(f"/web3/content/public/{body['content_id']}")
    assert public.status_code == 200
    assert public.json()["simverse"]["resident_id"] == resident.id
    assert "immutable" in public.headers["cache-control"]


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
