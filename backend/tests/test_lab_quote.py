import pytest

from app.config import settings
from app.models.user import User
from app.services.auth_service import create_token
from app.services import lab_task_service
from app.services.lab_task_service import supported_scopes_for_adapter


def test_codex_scope_catalog_matches_no_egress_runtime():
    assert supported_scopes_for_adapter("codex") == ["code"]
    assert supported_scopes_for_adapter("mock") == [
        "web_search", "browse", "code", "http"
    ]


@pytest.mark.anyio
async def test_lab_quote_is_server_authoritative(client, db_session, monkeypatch):
    user = User(id="quote-user", name="Quote User", email="quote@example.com")
    db_session.add(user)
    await db_session.commit()
    monkeypatch.setattr(settings, "lab_pro_min_reward_sc", 100)
    monkeypatch.setattr(settings, "lab_platform_fee_rate", 0.1)
    monkeypatch.setattr(settings, "lab_sc_per_usd", 100)
    monkeypatch.setattr(settings, "lab_pro_budget_usd", 0.5)
    monkeypatch.setattr(settings, "lab_adapter", "codex")
    monkeypatch.setattr(settings, "jwt_secret", "quote-test-secret-that-is-32-bytes-long")

    response = await client.post(
        "/lab/quote",
        json={"reward_sc": 100, "scopes": ["code"]},
        headers={"Authorization": f"Bearer {create_token(user.id)}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "reward_sc": 100,
        "platform_fee_sc": 10,
        "total_hold_sc": 110,
        "minimum_reward_sc": 20,
        "eligible": True,
        "adapter": "codex",
        "available_scopes": ["code"],
        "unsupported_scopes": [],
        "model_tier": "high",
        "model_name": "deepseek-v4-pro",
        "model_policy_version": settings.lab_model_policy_version,
        "resource_cpu_cores": 4,
        "resource_memory_mb": 4096,
        "budget_usd_cents": 50,
        "pro_min_reward_sc": 100,
    }


@pytest.mark.anyio
async def test_lab_quote_requires_auth(client):
    response = await client.post(
        "/lab/quote", json={"reward_sc": 50, "scopes": ["code"]}
    )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_lab_quote_rejects_capabilities_missing_from_codex_runtime(
    client, db_session, monkeypatch
):
    user = User(id="scope-user", name="Scope User", email="scope@example.com")
    db_session.add(user)
    await db_session.commit()
    monkeypatch.setattr(settings, "lab_adapter", "codex")
    monkeypatch.setattr(settings, "jwt_secret", "quote-test-secret-that-is-32-bytes-long")

    response = await client.post(
        "/lab/quote",
        json={"reward_sc": 50, "scopes": ["web_search"]},
        headers={"Authorization": f"Bearer {create_token(user.id)}"},
    )

    assert response.status_code == 200
    assert response.json()["eligible"] is False
    assert response.json()["available_scopes"] == ["code"]
    assert response.json()["unsupported_scopes"] == ["web_search"]


@pytest.mark.anyio
async def test_task_creation_rejects_scope_the_runtime_cannot_execute(monkeypatch):
    monkeypatch.setattr(settings, "lab_adapter", "codex")
    monkeypatch.setattr(lab_task_service, "_require_execution_consumer", lambda: 1)

    with pytest.raises(lab_task_service.LabTaskError, match="does not support"):
        await lab_task_service.create_task(
            None,
            issuer_id="scope-user",
            title="Search the web",
            brief="Use current sources",
            scopes=["web_search"],
            reward_sc=50,
        )
