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


# --------------------------------------------------------------------------- #
# Task 4 — track B: civic poll threshold / quorum + the `policy` effect type    #
# --------------------------------------------------------------------------- #

def _poll(question, opts, *, threshold=None, quorum=False, key=None,
          value=None, tier="simple_majority"):
    """Build a closed-ready poll with seeded NPC tallies (deterministic — no
    RNG on this path; votes are the seeded input)."""
    from datetime import datetime, timedelta, UTC
    from app.models.season import Poll
    from app.services.policy_service import (
        META_KEY, META_THRESHOLD, META_QUORUM,
    )

    options = []
    for i, (label, votes) in enumerate(opts):
        o = {"label": label, "npc_votes": votes, "effect": None}
        if i == 0 and key is not None:
            o["effect"] = {"type": "policy", "key": key, "value": value,
                           "tier": tier}
        options.append(o)
    if threshold is not None:
        options[0][META_KEY] = key
        options[0][META_THRESHOLD] = threshold
        options[0][META_QUORUM] = quorum
    return Poll(question=question, options_json=options, status="open",
                closes_at=datetime.now(UTC) - timedelta(hours=1))


def _npcs(n):
    from app.models.resident import Resident
    return [Resident(slug=f"npc-{i}", name=f"NPC{i}", district="central_plaza",
                     status="idle", resident_type="npc", creator_id="sys",
                     tile_x=70, tile_y=56) for i in range(n)]


@pytest.mark.anyio
async def test_close_one_simple_majority_below_threshold_no_apply(
        db_session, approval_gate):
    """Winner takes a plurality but <50% → 流会, policy untouched.

    Under today's pure plurality the same tally would have executed."""
    from app.services import civic_service

    await _seed(db_session)
    poll = _poll("税率", [("加税", 4), ("维持", 3), ("减税", 3)],
                 threshold=0.50, key="tax_rate", value=0.3)
    db_session.add(poll)
    await db_session.commit()

    await civic_service._close_one(db_session, poll)
    await db_session.refresh(poll)
    assert poll.status == "closed"
    assert poll.options_json[0].get("won") is None
    assert poll.options_json[0]["_policy_outcome"] == "threshold_not_met"
    row = (await db_session.execute(
        select(Policy).where(Policy.key == "tax_rate"))).scalar_one()
    assert row.version == 1


@pytest.mark.anyio
async def test_close_one_simple_majority_above_threshold_applies(
        db_session, approval_gate):
    from app.services import civic_service
    from app.services.policy_service import PolicyService

    await _seed(db_session)
    poll = _poll("税率", [("加税", 7), ("维持", 3)],
                 threshold=0.50, key="tax_rate", value=0.3)
    db_session.add(poll)
    await db_session.commit()

    await civic_service._close_one(db_session, poll)
    await db_session.refresh(poll)
    assert poll.options_json[0]["won"] is True
    row = (await db_session.execute(
        select(Policy).where(Policy.key == "tax_rate"))).scalar_one()
    assert row.version == 2
    assert row.updated_by == f"poll:{poll.id}"
    assert await PolicyService(db_session).get("tax_rate") == 0.3


@pytest.mark.anyio
async def test_close_one_absolute_majority_quorum_and_supermajority(
        db_session, approval_gate):
    """Both bars must clear: quorum AND ≥66.7%; missing either → 流会."""
    from app.services import civic_service

    db_session.add_all(_npcs(10))
    await db_session.commit()
    await _seed(db_session)

    # (a) quorum met (8/10 ≥ 50%) but share 5/8 = 62.5% < 66.7% → 流会
    p1 = _poll("选举间隔", [("延长", 5), ("维持", 3)], threshold=0.667,
               quorum=True, key="election_interval_days", value=35,
               tier="absolute_majority")
    db_session.add(p1)
    await db_session.commit()
    await civic_service._close_one(db_session, p1)
    await db_session.refresh(p1)
    assert p1.options_json[0]["_policy_outcome"] == "threshold_not_met"

    # (b) share 3/4 = 75% ≥ 66.7% but only 4 of 10 voted → quorum 流会
    p2 = _poll("选举间隔", [("延长", 3), ("维持", 1)], threshold=0.667,
               quorum=True, key="election_interval_days", value=35,
               tier="absolute_majority")
    db_session.add(p2)
    await db_session.commit()
    await civic_service._close_one(db_session, p2)
    await db_session.refresh(p2)
    assert p2.options_json[0]["_policy_outcome"] == "quorum_not_met"

    row = (await db_session.execute(select(Policy).where(
        Policy.key == "election_interval_days"))).scalar_one()
    assert row.version == 1

    # (c) both bars clear → executes
    p3 = _poll("选举间隔", [("延长", 7), ("维持", 1)], threshold=0.667,
               quorum=True, key="election_interval_days", value=35,
               tier="absolute_majority")
    db_session.add(p3)
    await db_session.commit()
    await civic_service._close_one(db_session, p3)
    row = (await db_session.execute(select(Policy).where(
        Policy.key == "election_interval_days"))).scalar_one()
    assert row.version == 2


@pytest.mark.anyio
async def test_execute_outcome_policy_effect_atomic(db_session, approval_gate):
    """The `policy` effect writes through the conditional UPDATE: version is
    strictly monotonic, one increment per successful outcome."""
    from app.services import civic_service

    await _seed(db_session)
    for expected in (2, 3, 4):
        ok = await civic_service._execute_outcome(
            db_session, {"type": "policy", "key": "curfew_hours",
                         "value": [22, expected]}, poll_id=expected)
        assert ok is True
        row = (await db_session.execute(
            select(Policy).where(Policy.key == "curfew_hours"))).scalar_one()
        assert row.version == expected
        assert row.updated_by == f"poll:{expected}"


@pytest.mark.anyio
async def test_execute_outcome_policy_core_refused(db_session, approval_gate):
    """Not even a referendum can amend a constitutional_core entry."""
    from app.services import civic_service

    await _seed(db_session)
    ok = await civic_service._execute_outcome(
        db_session, {"type": "policy", "key": "exile_right", "value": False},
        poll_id=1)
    assert ok is False
    row = (await db_session.execute(
        select(Policy).where(Policy.key == "exile_right"))).scalar_one()
    assert row.version == 1


@pytest.mark.anyio
async def test_approval_gate_off_track_b_plurality(db_session, policy_gate):
    """Gate off → _close_one is pure plurality again: the same <50% tally that
    was 流会 above now executes, and the `policy` effect type is unknown."""
    from app.services import civic_service

    assert settings.polis_policy_approval_enabled is False
    await _seed(db_session)
    poll = _poll("税率", [("加税", 4), ("维持", 3), ("减税", 3)],
                 threshold=0.50, key="tax_rate", value=0.3)
    db_session.add(poll)
    await db_session.commit()

    await civic_service._close_one(db_session, poll)
    await db_session.refresh(poll)
    assert poll.options_json[0]["won"] is True         # plurality wins
    assert "_policy_outcome" not in poll.options_json[0]
    # ...but the policy effect type is not recognized → nothing written
    row = (await db_session.execute(
        select(Policy).where(Policy.key == "tax_rate"))).scalar_one()
    assert row.version == 1


@pytest.mark.anyio
async def test_close_one_non_policy_poll_keeps_plurality(db_session,
                                                          approval_gate):
    """Even with the gate on, an ordinary civic poll (no tier metadata) keeps
    pure plurality — the threshold only binds tier-governed polls."""
    from app.services import civic_service

    poll = _poll("要不要办灯会", [("办", 2), ("不办", 1), ("再议", 2)])
    db_session.add(poll)
    await db_session.commit()
    await civic_service._close_one(db_session, poll)
    await db_session.refresh(poll)
    assert poll.options_json[0]["won"] is True         # 2/5 = 40% still wins


@pytest.mark.anyio
async def test_close_due_polls_covers_policy_polls(db_session, approval_gate):
    """No new nightly block is needed: the existing M3 close_due_polls sweep
    closes tier-governed polls too (KICKOFF §2 任务 4 "若已覆盖则复用")."""
    from app.services import civic_service

    await _seed(db_session)
    poll = _poll("税率", [("加税", 9), ("维持", 1)], threshold=0.50,
                 key="tax_rate", value=0.25)
    db_session.add(poll)
    await db_session.commit()

    assert await civic_service.close_due_polls(db_session) == 1
    row = (await db_session.execute(
        select(Policy).where(Policy.key == "tax_rate"))).scalar_one()
    assert row.version == 2


@pytest.mark.anyio
async def test_policy_probe_data_never_enters_npc_prompt(db_session,
                                                          approval_gate):
    """红线 §9.3 prompt 隔离: no prompt builder may reference policy/probe
    symbols. Static assertion over the whole prompt layer."""
    import pathlib

    root = pathlib.Path(civic_prompt_root())
    hits = []
    for path in root.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        if ("policy_service" in src or "PolicyService" in src
                or "policy_core_touch" in src or "policy_drift" in src):
            hits.append(str(path))
    assert hits == [], f"policy metrics leaked into the prompt layer: {hits}"


def civic_prompt_root() -> str:
    import app.agent as agent_pkg
    import pathlib
    return str(pathlib.Path(agent_pkg.__file__).parent)
