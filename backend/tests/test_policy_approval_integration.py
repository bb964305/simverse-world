"""S2-5 四级分级审批 — integration tests (KICKOFF_S2-5_policies.md §5).

Track A (admin console + proposal_service tier door) and track B (civic poll
threshold + the ``policy`` effect type), plus the two gate-off doors that must
leave both governance lifecycles byte-level unchanged.
"""
import pytest
from sqlalchemy import select

from app.config import settings
from app.models.policy import Policy
from app.models.user import User
from app.services.auth_service import create_token


@pytest.fixture
def policy_gate(monkeypatch):
    monkeypatch.setattr(settings, "polis_policy_enabled", True)
    return settings


@pytest.fixture
def approval_gate(monkeypatch):
    monkeypatch.setattr(settings, "polis_policy_enabled", True)
    monkeypatch.setattr(settings, "polis_policy_approval_enabled", True)
    return settings


async def _admin(db) -> tuple[User, dict]:
    u = User(name="root", email="root@test.com", is_admin=True, is_banned=False)
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u, {"Authorization": f"Bearer {create_token(u.id)}"}


async def _seed(db):
    from app.services.policy_service import PolicyService
    return await PolicyService(db).seed_defaults()


# --------------------------------------------------------------------------- #
# Task 3 — track A: admin console + proposal_service tier door                 #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_admin_amend_administrative_applies(client, db_session, policy_gate):
    _, headers = await _admin(db_session)
    await _seed(db_session)

    r = await client.post("/admin/policies/market_day_discount/amend",
                          json={"value": 0.8, "expected_version": 1},
                          headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2

    row = (await db_session.execute(
        select(Policy).where(Policy.key == "market_day_discount"))).scalar_one()
    assert row.version == 2
    assert row.updated_by.startswith("admin:")


@pytest.mark.anyio
async def test_admin_amend_non_administrative_409(client, db_session, policy_gate):
    """A vote-tier entry cannot be applied by a single admin."""
    _, headers = await _admin(db_session)
    await _seed(db_session)

    r = await client.post("/admin/policies/tax_rate/amend",
                          json={"value": 0.5, "expected_version": 1},
                          headers=headers)
    assert r.status_code == 409
    assert "civic_poll" in r.json()["detail"]
    row = (await db_session.execute(
        select(Policy).where(Policy.key == "tax_rate"))).scalar_one()
    assert row.version == 1


@pytest.mark.anyio
async def test_admin_amend_constitutional_core_409(client, db_session, policy_gate):
    _, headers = await _admin(db_session)
    await _seed(db_session)

    r = await client.post("/admin/policies/election_exists/amend",
                          json={"value": False, "expected_version": 1},
                          headers=headers)
    assert r.status_code == 409
    assert "constitutional_core" in r.json()["detail"]
    row = (await db_session.execute(
        select(Policy).where(Policy.key == "election_exists"))).scalar_one()
    assert row.version == 1        # 核心条款漂移恒 0


@pytest.mark.anyio
async def test_admin_amend_requires_admin(client, db_session, policy_gate):
    """Per-endpoint Depends(require_admin): no credential → 401, non-admin → 403."""
    r = await client.post("/admin/policies/market_day_discount/amend",
                          json={"value": 0.8})
    assert r.status_code == 401

    u = User(name="joe", email="joe@test.com", is_admin=False, is_banned=False)
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    r = await client.post("/admin/policies/market_day_discount/amend",
                          json={"value": 0.8},
                          headers={"Authorization": f"Bearer {create_token(u.id)}"})
    assert r.status_code == 403

    r = await client.get("/admin/policies")
    assert r.status_code == 401


@pytest.mark.anyio
async def test_admin_list_and_seed_endpoints(client, db_session, policy_gate):
    _, headers = await _admin(db_session)

    r = await client.post("/admin/policies/seed", headers=headers)
    assert r.status_code == 200
    assert r.json()["inserted"] > 0
    # idempotent
    assert (await client.post("/admin/policies/seed", headers=headers)
            ).json()["inserted"] == 0

    r = await client.get("/admin/policies", headers=headers)
    body = r.json()
    assert body["enabled"] is True
    tiers = {p["tier"] for p in body["policies"]}
    assert tiers == {"administrative", "simple_majority",
                     "absolute_majority", "constitutional_core"}
    assert body["matrix"]["constitutional_core"]["path"] == "immutable"


@pytest.mark.anyio
async def test_admin_endpoints_gate_off(client, db_session):
    """Storage gate off → list is an empty projection, amend/seed are 409."""
    _, headers = await _admin(db_session)
    assert settings.polis_policy_enabled is False

    r = await client.get("/admin/policies", headers=headers)
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "matrix": r.json()["matrix"],
                        "policies": []}
    assert (await client.post("/admin/policies/seed", headers=headers)
            ).status_code == 409
    assert (await client.post("/admin/policies/market_day_discount/amend",
                              json={"value": 1}, headers=headers)
            ).status_code == 409


@pytest.mark.anyio
async def test_townhall_policies_player_readonly(client, db_session, policy_gate):
    """§6 玩家只读 projection — no auth required, read-only, fail-open."""
    await _seed(db_session)
    r = await client.get("/townhall/policies")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert len(body["policies"]) > 0
    assert body["tiers"]["simple_majority"]["path"] == "civic_poll"


@pytest.mark.anyio
async def test_townhall_policies_gate_off_empty(client, db_session):
    assert settings.polis_policy_enabled is False
    body = (await client.get("/townhall/policies")).json()
    assert body["enabled"] is False
    assert body["policies"] == []


@pytest.mark.anyio
async def test_approve_proposal_policy_tier_door_blocks_vote_tier(
        db_session, approval_gate):
    """A proposal carrying a vote-tier policy_key cannot be admin-approved."""
    from app.models.world_change_proposal import WorldChangeProposal
    from app.services import proposal_service as psvc

    p = WorldChangeProposal(
        origin="admin", kind="add_lore", title="改税率",
        patch_json={"policy_key": "tax_rate", "value": 0.3},
        status="pending", risk_level="low", cost_sc=0,
    )
    db_session.add(p)
    await db_session.commit()

    with pytest.raises(psvc.ProposalError) as e:
        await psvc.approve_proposal(db_session, p.id, "admin-1")
    assert "civic_poll" in str(e.value)
    await db_session.refresh(p)
    assert p.status == "pending"        # untouched, no CAS ran


@pytest.mark.anyio
async def test_approve_proposal_policy_tier_door_blocks_core(
        db_session, approval_gate):
    from app.models.world_change_proposal import WorldChangeProposal
    from app.services import proposal_service as psvc

    p = WorldChangeProposal(
        origin="admin", kind="add_lore", title="废除选举",
        patch_json={"policy_key": "election_exists", "value": False},
        status="pending", risk_level="low", cost_sc=0,
    )
    db_session.add(p)
    await db_session.commit()

    with pytest.raises(psvc.ProposalError) as e:
        await psvc.approve_proposal(db_session, p.id, "admin-1")
    assert "constitutional_core" in str(e.value)
    await db_session.refresh(p)
    assert p.status == "pending"


@pytest.mark.anyio
async def test_approve_proposal_policy_tier_door_allows_administrative(
        db_session, approval_gate):
    """An administrative entry still flows through the normal CAS→apply path."""
    from app.models.world_change_proposal import WorldChangeProposal
    from app.services import proposal_service as psvc

    p = WorldChangeProposal(
        origin="admin", kind="add_lore",
        title="集市日折扣", rationale_md="小额行政调整",
        patch_json={"policy_key": "market_day_discount", "value": 0.85,
                    "location_id": "town_hall",
                    "text": "集市日折扣调整为 85 折"},
        status="pending", risk_level="low", cost_sc=0,
    )
    db_session.add(p)
    await db_session.commit()

    out = await psvc.approve_proposal(db_session, p.id, "admin-1")
    assert out.status == "applied"


@pytest.mark.anyio
async def test_approval_gate_off_track_a_unchanged(db_session, policy_gate):
    """polis_policy_approval_enabled=False → the tier door is skipped entirely
    and a vote-tier policy proposal approves exactly like pre-S2-5."""
    from app.models.world_change_proposal import WorldChangeProposal
    from app.services import proposal_service as psvc

    assert settings.polis_policy_approval_enabled is False
    p = WorldChangeProposal(
        origin="admin", kind="add_lore", title="改税率",
        rationale_md="现状路径",
        patch_json={"policy_key": "tax_rate", "value": 0.3,
                    "location_id": "town_hall", "text": "税率 0.3"},
        status="pending", risk_level="low", cost_sc=0,
    )
    db_session.add(p)
    await db_session.commit()

    out = await psvc.approve_proposal(db_session, p.id, "admin-1")
    assert out.status == "applied"      # single-admin CAS→apply, unchanged
