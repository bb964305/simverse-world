"""S2-5 policies + 四级分级审批 — unit tests (KICKOFF_S2-5_policies.md §5).

Every gated path carries a gate-off assertion: with ``POLIS_POLICY_*`` off the
behavior must be byte-level identical to the status quo (``ConfigService`` on
``system_config``, no ``policies`` row ever touched).
"""
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import func, select

from app.config import settings
from app.models.policy import Policy


@pytest.fixture
def policy_gate(monkeypatch):
    """Turn the storage gate on for a test (default is False everywhere)."""
    monkeypatch.setattr(settings, "polis_policy_enabled", True)
    return settings


@pytest.fixture
def approval_gate(monkeypatch):
    """Turn the approval-routing gate on (independent of the storage gate)."""
    monkeypatch.setattr(settings, "polis_policy_enabled", True)
    monkeypatch.setattr(settings, "polis_policy_approval_enabled", True)
    return settings


async def _count(db):
    return (await db.execute(select(func.count()).select_from(Policy))).scalar()


# --------------------------------------------------------------------------- #
# Task 1 — table + model + migration                                           #
# --------------------------------------------------------------------------- #

def test_integration_migration_single_head():
    """`alembic heads` stays single-headed and the S2-5 migration chains onto
    the linearized head (048_add_town_treasury, S1-5 merged first)."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic multi-head: {heads}"
    rev = script.get_revision("049_add_policies")
    assert rev is not None
    assert rev.down_revision == "048_add_town_treasury"


def test_migration_creates_table_only_no_alter():
    """The migration must not ALTER any existing table — notably it must not
    add tier/procedure columns to world_change_proposals (KICKOFF §7)."""
    import ast

    path = (Path(__file__).resolve().parent.parent / "alembic" / "versions"
            / "049_add_policies.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # Compare code only — the module docstring legitimately names these APIs.
    body = [n for n in tree.body if not (isinstance(n, ast.Expr)
                                         and isinstance(n.value, ast.Constant))]
    code = "\n".join(ast.unparse(n) for n in body)
    assert "add_column" not in code
    assert "batch_alter_table" not in code
    assert "world_change_proposals" not in code


@pytest.mark.anyio
async def test_policies_table_created(db_engine):
    """models/__init__.py registers Policy so Base.metadata.create_all sees it."""
    async with db_engine.connect() as conn:
        names = await conn.run_sync(lambda sc: sa.inspect(sc).get_table_names())
    assert "policies" in names


@pytest.mark.anyio
async def test_policy_key_is_unique(db_session):
    """One row per key — the amend path relies on it for the conditional
    UPDATE to touch at most one row."""
    from sqlalchemy.exc import IntegrityError

    db_session.add(Policy(key="k", value="1", tier="administrative",
                          procedure="admin_direct", group="civic"))
    await db_session.commit()
    db_session.add(Policy(key="k", value="2", tier="administrative",
                          procedure="admin_direct", group="civic"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.anyio
async def test_policy_version_defaults_to_one(db_session):
    db_session.add(Policy(key="v", value="1", tier="administrative",
                          procedure="admin_direct", group="civic"))
    await db_session.commit()
    row = (await db_session.execute(
        select(Policy).where(Policy.key == "v"))).scalar_one()
    assert row.version == 1
    assert row.created_at is not None and row.updated_at is not None


@pytest.mark.anyio
async def test_policy_value_holds_more_than_system_config_limit(db_session):
    """The 2000-char ceiling on system_config.value is why this table exists."""
    big = "x" * 5000
    db_session.add(Policy(key="big", value=f'"{big}"', tier="simple_majority",
                          procedure="civic_poll", group="civic"))
    await db_session.commit()
    row = (await db_session.execute(
        select(Policy).where(Policy.key == "big"))).scalar_one()
    assert len(row.value) == 5002


# --------------------------------------------------------------------------- #
# Task 2 — PolicyService: matrix, seeding, atomic amend, routing               #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_seed_defaults_idempotent(db_session, policy_gate):
    """Two calls: the second inserts nothing and the row count is unchanged."""
    from app.services.policy_service import PolicyService, POLICY_CATALOG

    svc = PolicyService(db_session)
    first = await svc.seed_defaults()
    assert first == len(POLICY_CATALOG)
    n_after_first = await _count(db_session)

    second = await svc.seed_defaults()
    assert second == 0
    assert await _count(db_session) == n_after_first


@pytest.mark.anyio
async def test_seed_defaults_gate_off_writes_nothing(db_session):
    """polis_policy_enabled=False → seed_defaults is a no-op (0 rows)."""
    from app.services.policy_service import PolicyService

    assert settings.polis_policy_enabled is False
    assert await PolicyService(db_session).seed_defaults() == 0
    assert await _count(db_session) == 0


@pytest.mark.anyio
async def test_seed_defaults_stamps_tier_and_procedure(db_session, policy_gate):
    from app.services.policy_service import PolicyService, TIER_MATRIX

    await PolicyService(db_session).seed_defaults()
    rows = (await db_session.execute(select(Policy))).scalars().all()
    assert rows
    for r in rows:
        assert r.tier in TIER_MATRIX
        assert r.procedure == TIER_MATRIX[r.tier]["path"]
        assert r.version == 1
        assert r.updated_by == "seed"


@pytest.mark.anyio
async def test_apply_amend_optimistic_version_wins(db_session, policy_gate):
    from app.services.policy_service import PolicyService

    svc = PolicyService(db_session)
    await svc.seed_defaults()
    ok = await svc.apply_amend("tax_rate", 0.12, expected_version=1,
                               updated_by="admin:1")
    assert ok is True
    row = (await db_session.execute(
        select(Policy).where(Policy.key == "tax_rate"))).scalar_one()
    assert row.version == 2
    assert await svc.get("tax_rate") == 0.12
    assert row.updated_by == "admin:1"


@pytest.mark.anyio
async def test_apply_amend_stale_version_loses(db_session, policy_gate):
    """A stale expected_version loses and leaves the value untouched."""
    from app.services.policy_service import PolicyService

    svc = PolicyService(db_session)
    await svc.seed_defaults()
    assert await svc.apply_amend("tax_rate", 0.12, expected_version=1,
                                 updated_by="a") is True
    assert await svc.apply_amend("tax_rate", 0.99, expected_version=1,
                                 updated_by="b") is False
    row = (await db_session.execute(
        select(Policy).where(Policy.key == "tax_rate"))).scalar_one()
    assert row.version == 2
    assert await svc.get("tax_rate") == 0.12   # no lost update


@pytest.mark.anyio
async def test_apply_amend_concurrent_no_lost_update(db_session, policy_gate):
    """Two writers racing on the same expected_version: exactly one wins.

    A shared in-memory sqlite engine serializes on one connection, so the race
    is expressed as two amends carrying the *same* expected_version — which is
    exactly the state two concurrent workers would hold after both read v1.
    """
    from app.services.policy_service import PolicyService

    svc = PolicyService(db_session)
    await svc.seed_defaults()
    results = [
        await svc.apply_amend("curfew_hours", [22, 6], expected_version=1,
                              updated_by="w1"),
        await svc.apply_amend("curfew_hours", [23, 5], expected_version=1,
                              updated_by="w2"),
    ]
    assert sum(results) == 1
    row = (await db_session.execute(
        select(Policy).where(Policy.key == "curfew_hours"))).scalar_one()
    assert row.version == 2            # exactly one increment, never two
    assert await svc.get("curfew_hours") == [22, 6]


@pytest.mark.anyio
async def test_apply_amend_auto_version_reads_then_cas(db_session, policy_gate):
    """expected_version=None → read current, then conditional UPDATE on it."""
    from app.services.policy_service import PolicyService

    svc = PolicyService(db_session)
    await svc.seed_defaults()
    assert await svc.apply_amend("curfew_hours", [1, 2], updated_by="poll:7") is True
    assert await svc.apply_amend("curfew_hours", [3, 4], updated_by="poll:8") is True
    row = (await db_session.execute(
        select(Policy).where(Policy.key == "curfew_hours"))).scalar_one()
    assert row.version == 3


@pytest.mark.anyio
async def test_apply_amend_gate_off_writes_nothing(db_session, policy_gate,
                                                   monkeypatch):
    from app.services.policy_service import PolicyService

    svc = PolicyService(db_session)
    await svc.seed_defaults()
    monkeypatch.setattr(settings, "polis_policy_enabled", False)
    assert await svc.apply_amend("tax_rate", 9.9, expected_version=1,
                                 updated_by="x") is False
    monkeypatch.setattr(settings, "polis_policy_enabled", True)
    row = (await db_session.execute(
        select(Policy).where(Policy.key == "tax_rate"))).scalar_one()
    assert row.version == 1


@pytest.mark.anyio
async def test_classify_returns_tier(db_session, policy_gate):
    from app.services.policy_service import PolicyService

    svc = PolicyService(db_session)
    assert await svc.classify("market_day_discount") == "administrative"
    assert await svc.classify("tax_rate") == "simple_majority"
    assert await svc.classify("election_interval_days") == "absolute_majority"
    assert await svc.classify("approval_routing") == "absolute_majority"
    assert await svc.classify("election_exists") == "constitutional_core"
    # unknown key → conservative fallback, never admin_direct
    assert await svc.classify("some_unheard_of_key") == "simple_majority"


@pytest.mark.anyio
async def test_classify_row_wins_over_catalog(db_session, policy_gate):
    from app.services.policy_service import PolicyService

    db_session.add(Policy(key="tax_rate", value="0.0", tier="absolute_majority",
                          procedure="civic_poll_supermajority", group="fiscal"))
    await db_session.commit()
    assert await PolicyService(db_session).classify("tax_rate") == "absolute_majority"


@pytest.mark.anyio
async def test_propose_amend_administrative_routes_admin(db_session, policy_gate):
    from app.services.policy_service import PolicyService

    res = await PolicyService(db_session).propose_amend(
        "market_day_discount", 0.8, origin="admin", author="admin:1")
    assert res.tier == "administrative"
    assert res.path == "admin_direct"
    assert res.poll_id is None
    assert res.threshold is None


@pytest.mark.anyio
async def test_propose_amend_simple_majority_opens_poll(db_session, policy_gate):
    from app.models.season import Poll
    from app.services.policy_service import (
        PolicyService, META_KEY, META_THRESHOLD, META_QUORUM,
    )

    res = await PolicyService(db_session).propose_amend(
        "tax_rate", 0.1, origin="resident", author="jiang-lin")
    assert res.tier == "simple_majority"
    assert res.path == "civic_poll"
    assert res.threshold == 0.50
    assert res.quorum is False
    assert res.poll_id is not None

    poll = await db_session.get(Poll, res.poll_id)
    blob = poll.options_json[0]
    assert blob[META_KEY] == "tax_rate"
    assert blob[META_THRESHOLD] == 0.50
    assert blob[META_QUORUM] is False
    assert poll.options_json[0]["effect"]["type"] == "policy"


@pytest.mark.anyio
async def test_propose_amend_absolute_majority_sets_supermajority(db_session,
                                                                  policy_gate):
    from app.models.season import Poll
    from app.services.policy_service import (
        PolicyService, META_THRESHOLD, META_QUORUM,
    )

    res = await PolicyService(db_session).propose_amend(
        "election_interval_days", 35, origin="resident", author="jiang-lin")
    assert res.tier == "absolute_majority"
    assert res.path == "civic_poll_supermajority"
    assert res.threshold == 0.667
    assert res.quorum is True

    poll = await db_session.get(Poll, res.poll_id)
    blob = poll.options_json[0]
    assert blob[META_THRESHOLD] == 0.667
    assert blob[META_QUORUM] is True


@pytest.mark.anyio
async def test_propose_amend_constitutional_core_rejected(db_session, policy_gate):
    """§3.3:97 — core entries are immutable: raise, no poll, no write."""
    from sqlalchemy import func as _func
    from app.models.season import Poll
    from app.services.policy_service import PolicyService, PolicyImmutableError

    svc = PolicyService(db_session)
    await svc.seed_defaults()
    before = (await db_session.execute(
        select(Policy).where(Policy.key == "election_exists"))).scalar_one()
    assert before.version == 1

    with pytest.raises(PolicyImmutableError):
        await svc.propose_amend("election_exists", False,
                                origin="admin", author="admin:1")

    polls = (await db_session.execute(
        select(_func.count()).select_from(Poll))).scalar()
    assert polls == 0
    after = (await db_session.execute(
        select(Policy).where(Policy.key == "election_exists"))).scalar_one()
    assert after.version == 1            # 核心条款漂移恒 0


@pytest.mark.anyio
async def test_apply_amend_constitutional_core_rejected(db_session, policy_gate):
    """Direct apply on a core entry raises too — not just the propose route."""
    from app.services.policy_service import PolicyService, PolicyImmutableError

    svc = PolicyService(db_session)
    await svc.seed_defaults()
    with pytest.raises(PolicyImmutableError):
        await svc.apply_amend("lab_approval_gate", False, expected_version=1,
                              updated_by="admin:1")
    row = (await db_session.execute(
        select(Policy).where(Policy.key == "lab_approval_gate"))).scalar_one()
    assert row.version == 1
    assert await svc.get("lab_approval_gate") is True


@pytest.mark.anyio
async def test_core_touch_attempts_counted_successes_zero(db_session, policy_gate):
    """§6 probe: attempts may be >0, successes are always 0."""
    from app.services.config_service import ConfigService
    from app.services.policy_service import (
        PolicyService, PolicyImmutableError, CORE_TOUCH_KEY,
    )

    svc = PolicyService(db_session)
    await svc.seed_defaults()
    for _ in range(3):
        with pytest.raises(PolicyImmutableError):
            await svc.propose_amend("exile_right", False,
                                    origin="resident", author="jiang-lin")
    counter = await ConfigService(db_session).get(CORE_TOUCH_KEY)
    assert counter["attempts"] == 3
    assert counter["by_key"]["exile_right"] == 3

    core_versions = (await db_session.execute(
        select(Policy.version).where(Policy.tier == "constitutional_core")
    )).scalars().all()
    assert core_versions and set(core_versions) == {1}   # 成功数恒 = 0


@pytest.mark.anyio
async def test_gate_off_falls_back_to_config_service(db_session):
    """polis_policy_enabled=False → get/get_group read system_config and the
    policies table is never touched (byte-level status quo)."""
    from app.services.config_service import ConfigService
    from app.services.policy_service import PolicyService

    assert settings.polis_policy_enabled is False
    await ConfigService(db_session).set("tax_rate", 0.07, group="fiscal",
                                        updated_by="test")
    # A stale policies row must be invisible while the gate is off.
    db_session.add(Policy(key="tax_rate", value="0.42", tier="simple_majority",
                          procedure="civic_poll", group="fiscal"))
    await db_session.commit()

    svc = PolicyService(db_session)
    assert await svc.get("tax_rate") == 0.07
    assert await svc.get_group("fiscal") == {"tax_rate": 0.07}
    assert await svc.list_all() == []


@pytest.mark.anyio
async def test_gate_on_get_falls_back_on_miss(db_session, policy_gate):
    """Migration-period coexistence: a key with no policies row still reads
    from system_config."""
    from app.services.config_service import ConfigService
    from app.services.policy_service import PolicyService

    await ConfigService(db_session).set("current_mayor", "he-qiaoyun",
                                        group="civic", updated_by="test")
    assert await PolicyService(db_session).get("current_mayor") == "he-qiaoyun"
    assert await PolicyService(db_session).get("nope", default=7) == 7


@pytest.mark.anyio
async def test_approval_routing_is_absolute_majority_self_reference(db_session,
                                                                    policy_gate):
    """§3.3 自指保护: the routing rules themselves sit at the supermajority
    tier and their seeded value is the tier→path map."""
    from app.services.policy_service import PolicyService, TIER_MATRIX

    svc = PolicyService(db_session)
    await svc.seed_defaults()
    assert await svc.classify("approval_routing") == "absolute_majority"
    assert await svc.get("approval_routing") == {
        t: spec["path"] for t, spec in TIER_MATRIX.items()
    }


def test_tier_matrix_shape_matches_spec():
    from app.services.policy_service import TIER_MATRIX, DEFAULT_TIER

    assert set(TIER_MATRIX) == {
        "administrative", "simple_majority",
        "absolute_majority", "constitutional_core",
    }
    assert TIER_MATRIX["administrative"]["path"] == "admin_direct"
    assert TIER_MATRIX["simple_majority"]["path"] == "civic_poll"
    assert TIER_MATRIX["absolute_majority"]["path"] == "civic_poll_supermajority"
    assert TIER_MATRIX["absolute_majority"]["quorum"] is True
    assert TIER_MATRIX["constitutional_core"]["path"] == "immutable"
    assert TIER_MATRIX["constitutional_core"]["authority"] == "none"
    assert DEFAULT_TIER == "simple_majority"       # never admin_direct


def test_fiscal_pending_keys_registered():
    """S1-5 待接线清单 must stay in sync with the catalog (report §待接线)."""
    from app.services.policy_service import FISCAL_PENDING_KEYS, CATALOG_BY_KEY

    assert FISCAL_PENDING_KEYS <= set(CATALOG_BY_KEY)
    for key in FISCAL_PENDING_KEYS:
        assert CATALOG_BY_KEY[key]["group"] == "fiscal"
