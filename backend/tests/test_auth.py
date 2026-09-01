import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

@pytest.mark.anyio
async def test_register_email(client):
    resp = await client.post("/auth/register", json={
        "name": "TestUser",
        "email": "test@example.com",
        "password": "securepass123"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data["user"]["name"] == "TestUser"
    assert data["user"]["soul_coin_balance"] == 100

@pytest.mark.anyio
async def test_login_email(client):
    await client.post("/auth/register", json={
        "name": "TestUser", "email": "login@example.com", "password": "securepass123"
    })
    resp = await client.post("/auth/login", json={
        "email": "login@example.com", "password": "securepass123"
    })
    assert resp.status_code == 200
    assert "access_token" in resp.json()

@pytest.mark.anyio
async def test_login_wrong_password(client):
    await client.post("/auth/register", json={
        "name": "U", "email": "wrong@example.com", "password": "correct"
    })
    resp = await client.post("/auth/login", json={
        "email": "wrong@example.com", "password": "incorrect"
    })
    assert resp.status_code == 401

@pytest.mark.anyio
async def test_get_me(client):
    reg = await client.post("/auth/register", json={
        "name": "Me", "email": "me@example.com", "password": "pass123"
    })
    token = reg.json()["access_token"]
    resp = await client.get("/users/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "me@example.com"

@pytest.mark.anyio
async def test_get_me_no_token(client):
    resp = await client.get("/users/me")
    assert resp.status_code == 401

@pytest.mark.anyio
async def test_register_duplicate_email(client):
    await client.post("/auth/register", json={"name": "A", "email": "dup@example.com", "password": "pass123"})
    resp = await client.post("/auth/register", json={"name": "B", "email": "dup@example.com", "password": "pass123"})
    assert resp.status_code == 409


@pytest.mark.anyio
async def test_wallet_signature_creates_and_resumes_identity(client):
    account = Account.create()
    challenge = await client.post(
        "/auth/wallet/challenge",
        headers={"Origin": "http://localhost:5173"},
        json={"address": account.address, "chain_id": 46630},
    )
    assert challenge.status_code == 200
    challenge_data = challenge.json()
    assert "wants you to sign in with your Ethereum account" in challenge_data["message"]
    assert challenge_data["chain_name"] == "Robinhood Chain Testnet"

    signature = Account.sign_message(
        encode_defunct(text=challenge_data["message"]), account.key
    ).signature.hex()
    verified = await client.post(
        "/auth/wallet/verify",
        json={
            "address": account.address,
            "message": challenge_data["message"],
            "signature": signature,
            "nonce": challenge_data["nonce"],
            "chain_id": 46630,
        },
    )
    assert verified.status_code == 200
    payload = verified.json()
    assert payload["user"]["wallet_address"] == account.address.lower()
    assert payload["user"]["soul_coin_balance"] == 100

    me = await client.get(
        "/users/me", headers={"Authorization": f"Bearer {payload['access_token']}"}
    )
    assert me.status_code == 200
    assert me.json()["wallet_address"] == account.address.lower()


@pytest.mark.anyio
async def test_wallet_challenge_is_single_use(client):
    account = Account.create()
    challenge = await client.post(
        "/auth/wallet/challenge",
        headers={"Origin": "http://localhost:5173"},
        json={"address": account.address, "chain_id": 46630},
    )
    data = challenge.json()
    signature = Account.sign_message(
        encode_defunct(text=data["message"]), account.key
    ).signature.hex()
    request = {
        "address": account.address,
        "message": data["message"],
        "signature": signature,
        "nonce": data["nonce"],
        "chain_id": 46630,
    }

    assert (await client.post("/auth/wallet/verify", json=request)).status_code == 200
    replay = await client.post("/auth/wallet/verify", json=request)
    assert replay.status_code == 401
    assert "已过期或已使用" in replay.json()["detail"]


@pytest.mark.anyio
async def test_wallet_challenge_rejects_wrong_chain_and_origin(client):
    account = Account.create()
    wrong_chain = await client.post(
        "/auth/wallet/challenge",
        headers={"Origin": "http://localhost:5173"},
        json={"address": account.address, "chain_id": 1},
    )
    assert wrong_chain.status_code == 400
    assert "Robinhood Chain Testnet" in wrong_chain.json()["detail"]

    wrong_origin = await client.post(
        "/auth/wallet/challenge",
        headers={"Origin": "https://phishing.example"},
        json={"address": account.address, "chain_id": 46630},
    )
    assert wrong_origin.status_code == 400
    assert "来源" in wrong_origin.json()["detail"]
