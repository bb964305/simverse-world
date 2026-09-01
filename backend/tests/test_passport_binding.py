from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.models.resident import Resident
from app.models.user import User
from app.models.web3_agent_passport import Web3AgentPassport
from app.services.auth_service import create_token
from app.services.web3_registry_service import PassportVerificationError, VerifiedPassport


WALLET = "0x1234567890123456789012345678901234567890"
REGISTRY = "0x24f6f6be48066cbe0b54d741cd4b52862bb4b05c"
HASH = "0x" + "12" * 32
TX = "0x" + "34" * 32
URI = "https://simverse.space/api/web3/content/public/passport-metadata"


async def _wallet_player(db_session, *, suffix: str = "one"):
    user = User(
        id=f"passport-user-{suffix}",
        name="Passport Owner",
        email=f"passport-{suffix}@identity.simverse.world",
        hashed_password=None,
        wallet_address=WALLET,
    )
    resident = Resident(
        id=f"passport-resident-{suffix}",
        slug=f"p-passport-{suffix}",
        name="Nova",
        creator_id=user.id,
        resident_type="player",
        sprite_key="埃迪",
    )
    user.player_resident_id = resident.id
    db_session.add_all([user, resident])
    await db_session.commit()
    return user, resident


@pytest.mark.anyio
async def test_confirm_passport_persists_verified_binding_idempotently(
    client, db_session, monkeypatch
):
    user, resident = await _wallet_player(db_session)
    verify = AsyncMock(return_value=VerifiedPassport(
        agent_id="7",
        resident_key="0x" + "56" * 32,
        registry_address=REGISTRY,
        chain_id=4663,
    ))
    monkeypatch.setattr("app.routers.onboarding.verify_passport_registration", verify)
    headers = {"Authorization": f"Bearer {create_token(user.id)}"}
    body = {
        "resident_id": resident.id,
        "agent_id": "7",
        "transaction_hash": TX,
        "metadata_uri": URI,
        "metadata_hash": HASH,
    }

    first = await client.post("/onboarding/passport/confirm", headers=headers, json=body)
    second = await client.post("/onboarding/passport/confirm", headers=headers, json=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    count = await db_session.scalar(select(func.count()).select_from(Web3AgentPassport))
    assert count == 1
    check = await client.get("/onboarding/check", headers=headers)
    assert check.json()["passport"] == {
        "agent_id": "7",
        "resident_id": resident.id,
        "chain_id": 4663,
        "registry_address": REGISTRY,
        "transaction_hash": TX,
    }


@pytest.mark.anyio
async def test_confirm_passport_rejects_a_resident_outside_current_wallet_identity(
    client, db_session, monkeypatch
):
    user, _resident = await _wallet_player(db_session, suffix="owner")
    foreign = Resident(
        id="passport-resident-foreign",
        slug="p-passport-foreign",
        name="Foreign",
        creator_id="someone-else",
        resident_type="player",
        sprite_key="埃迪",
    )
    db_session.add(foreign)
    await db_session.commit()
    verify = AsyncMock()
    monkeypatch.setattr("app.routers.onboarding.verify_passport_registration", verify)

    response = await client.post(
        "/onboarding/passport/confirm",
        headers={"Authorization": f"Bearer {create_token(user.id)}"},
        json={
            "resident_id": foreign.id,
            "agent_id": "7",
            "transaction_hash": TX,
            "metadata_uri": URI,
            "metadata_hash": HASH,
        },
    )

    assert response.status_code == 404
    verify.assert_not_awaited()


@pytest.mark.anyio
async def test_confirm_passport_fails_closed_when_chain_verification_fails(
    client, db_session, monkeypatch
):
    user, resident = await _wallet_player(db_session, suffix="rpc")
    verify = AsyncMock(side_effect=PassportVerificationError("RPC unavailable"))
    monkeypatch.setattr("app.routers.onboarding.verify_passport_registration", verify)

    response = await client.post(
        "/onboarding/passport/confirm",
        headers={"Authorization": f"Bearer {create_token(user.id)}"},
        json={
            "resident_id": resident.id,
            "agent_id": "7",
            "transaction_hash": None,
            "metadata_uri": URI,
            "metadata_hash": HASH,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "RPC unavailable"
    count = await db_session.scalar(select(func.count()).select_from(Web3AgentPassport))
    assert count == 0
