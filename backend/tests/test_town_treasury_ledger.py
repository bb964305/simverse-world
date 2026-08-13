"""058 town ledger, atomic payroll, and the dark sustainable-funding gate."""
import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.config import settings
from app.models.resident import Resident
from app.models.town_treasury import TOWN_KEY, TownTreasury
from app.models.town_treasury_entry import TownTreasuryEntry
from app.services import coin_service, duty_service, treasury_service


MIGRATION = (
    Path(__file__).resolve().parent.parent
    / "alembic" / "versions" / "058_add_town_ledger.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_058", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _resident(slug: str, duty_key: str) -> Resident:
    return Resident(
        slug=slug,
        name=slug,
        district="town_hall",
        status="idle",
        resident_type="npc",
        tile_x=1,
        tile_y=1,
        meta_json={"duty": {"key": duty_key, "perks": {"wage_sc": 8}}},
    )


def test_058_is_on_the_single_linear_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic multi-head: {heads}"
    revision = script.get_revision("058_add_town_ledger")
    assert revision.down_revision == (
        "057_add_arc_template_key"
    )
    ancestry = {rev.revision for rev in script.walk_revisions("base", heads[0])}
    assert revision.revision in ancestry


def test_ledger_model_shape():
    cols = TownTreasuryEntry.__table__.columns
    assert TownTreasuryEntry.__tablename__ == "town_treasury_entries"
    assert isinstance(cols["amount_sc"].type, sa.Integer)
    assert isinstance(cols["balance_after_sc"].type, sa.Integer)
    assert cols["ref_key"].unique is True


def test_every_seeded_duty_has_an_explicit_funding_source():
    from seed.preset_characters import PRESET_CHARACTERS

    duties = [
        (row.get("meta_json") or {}).get("duty")
        for row in PRESET_CHARACTERS
    ]
    assert duties and all(d for d in duties)
    assert {d["funding_source"] for d in duties} <= {
        "public", "private", "none",
    }
    assert all(
        duty_service.DUTY_FUNDING_DEFAULTS[d["key"]] == d["funding_source"]
        for d in duties
    )


@pytest.mark.anyio
async def test_migration_anchors_existing_balance(db_engine, db_session):
    db_session.add(TownTreasury(
        key=TOWN_KEY, balance_sc=23, updated_at=datetime.now(UTC),
    ))
    await db_session.commit()

    migration = _load_migration()
    async with db_engine.begin() as conn:
        assert await conn.run_sync(migration._anchor_existing_balances) == 1

    row = (await db_session.execute(
        select(TownTreasuryEntry).where(
            TownTreasuryEntry.ref_key == f"opening_balance:{TOWN_KEY}"
        )
    )).scalar_one()
    assert (row.amount_sc, row.balance_after_sc, row.reason) == (
        23, 23, "opening_balance",
    )


@pytest.mark.anyio
async def test_ledger_off_no_entries_written(db_session):
    # 默认闸关(058 已落库但镜像写未开): 资金操作全部照常成功, 但
    # town_treasury_entries 一行不写——迁移与行为变更解耦(07-25 红线)。
    assert settings.town_ledger_enabled is False
    await treasury_service.tax(db_session, 10, reason="sales_tax:test")
    assert await treasury_service.disburse(db_session, 3, reason="public_works")
    assert await treasury_service.town_to_resident(
        db_session, "clerk", 2, reason="wage:clerk",
    )
    assert await treasury_service.balance(db_session) == 5
    assert await coin_service.treasury_balance(db_session, "clerk") == 2
    rows = (await db_session.execute(select(TownTreasuryEntry))).scalars().all()
    assert rows == []


@pytest.mark.anyio
async def test_tax_and_spend_record_returned_balances(db_session, monkeypatch):
    monkeypatch.setattr(settings, "town_ledger_enabled", True)
    await treasury_service.tax(db_session, 10, reason="sales_tax:test")
    assert await treasury_service.disburse(
        db_session, 3, reason="public_works"
    )

    rows = (await db_session.execute(
        select(TownTreasuryEntry).order_by(TownTreasuryEntry.created_at)
    )).scalars().all()
    assert [(r.amount_sc, r.balance_after_sc, r.reason) for r in rows] == [
        (10, 10, "sales_tax:test"),
        (-3, 7, "public_works"),
    ]
    assert await treasury_service.balance(db_session) == 7


@pytest.mark.anyio
async def test_town_to_resident_is_one_transaction(db_session, monkeypatch):
    monkeypatch.setattr(settings, "town_ledger_enabled", True)
    await treasury_service.tax(db_session, 10, reason="sales_tax:test")

    async def fail_credit(*args, **kwargs):
        raise RuntimeError("resident credit failed")

    monkeypatch.setattr(coin_service, "treasury_credit_pending", fail_credit)
    with pytest.raises(RuntimeError, match="resident credit failed"):
        await treasury_service.town_to_resident(
            db_session, "clerk", 4, reason="wage:clerk",
        )

    assert await treasury_service.balance(db_session) == 10
    assert await coin_service.treasury_balance(db_session, "clerk") == 0
    rows = (await db_session.execute(select(TownTreasuryEntry))).scalars().all()
    assert [(r.amount_sc, r.balance_after_sc) for r in rows] == [(10, 10)]


@pytest.mark.anyio
async def test_stable_ref_makes_transfer_retry_a_noop(db_session, monkeypatch):
    monkeypatch.setattr(settings, "town_ledger_enabled", True)
    await treasury_service.tax(db_session, 10, reason="sales_tax:test")
    assert await treasury_service.town_to_resident(
        db_session, "clerk", 2, reason="wage:clerk", ref_key="wage:run:1",
    )
    assert not await treasury_service.town_to_resident(
        db_session, "clerk", 2, reason="wage:clerk", ref_key="wage:run:1",
    )
    assert await treasury_service.balance(db_session) == 8
    assert await coin_service.treasury_balance(db_session, "clerk") == 2


@pytest.mark.anyio
async def test_transfer_noop_paths_release_the_locking_transaction(
    db_session, monkeypatch,
):
    monkeypatch.setattr(settings, "town_ledger_enabled", True)
    await treasury_service.tax(db_session, 10, reason="sales_tax:test")

    assert not await treasury_service.town_to_resident(
        db_session, "clerk", 99, reason="wage:clerk",
    )
    assert db_session.in_transaction() is False

    assert not await treasury_service.town_to_resident(
        db_session, "clerk", 1, reason="wage:clerk", wage_budget_ratio=0.0,
    )
    assert db_session.in_transaction() is False

    assert await treasury_service.town_to_resident(
        db_session, "clerk", 1, reason="wage:clerk", ref_key="wage:once",
    )
    assert not await treasury_service.town_to_resident(
        db_session, "clerk", 1, reason="wage:clerk", ref_key="wage:once",
    )
    assert db_session.in_transaction() is False


@pytest.mark.anyio
async def test_rolling_income_budget_keeps_thirty_percent_reserve(
    db_session, monkeypatch,
):
    monkeypatch.setattr(settings, "town_ledger_enabled", True)
    await treasury_service.tax(db_session, 10, reason="sales_tax:test")
    assert await treasury_service.town_to_resident(
        db_session, "clerk", 7, reason="wage:clerk", wage_budget_ratio=0.70,
    )
    assert not await treasury_service.town_to_resident(
        db_session, "postman", 1, reason="wage:postman", wage_budget_ratio=0.70,
    )
    assert await treasury_service.balance(db_session) == 3
    assert await treasury_service.wage_window_totals(db_session) == (10, 7)


@pytest.mark.anyio
async def test_funding_split_is_dark_then_public_only(db_session, monkeypatch):
    public = _resident("postman", "postman")
    private = _resident("smith", "workshop_fixer")
    db_session.add_all([public, private])
    await db_session.commit()

    monkeypatch.setattr(settings, "npc_economy_enabled", True)
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_ledger_enabled", True)
    monkeypatch.setattr(settings, "town_wage_unfunded_policy", "skip")
    monkeypatch.setattr(settings, "election_enabled", False)
    monkeypatch.setattr(settings, "town_duty_funding_enabled", False)
    await treasury_service.tax(db_session, 20, reason="sales_tax:legacy")
    await duty_service._pay_wage(db_session, private)
    assert await coin_service.treasury_balance(db_session, "smith") == 8

    monkeypatch.setattr(settings, "town_duty_funding_enabled", True)
    monkeypatch.setattr(settings, "town_public_duty_wage_sc", 1)
    monkeypatch.setattr(settings, "town_wage_budget_ratio", 0.70)
    await duty_service._pay_wage(db_session, private)
    await duty_service._pay_wage(db_session, public)

    # Private work received no second town wage; public output received the
    # configured flat wage through the sustainable budget.
    assert await coin_service.treasury_balance(db_session, "smith") == 8
    assert await coin_service.treasury_balance(db_session, "postman") == 1


@pytest.mark.anyio
async def test_sustainable_without_treasury_pays_nothing(db_session, monkeypatch):
    """F7 组合守卫: funding 闸开而 treasury 闸关时, public duty 不得绕过国库
    走 treasury_credit 凭空铸币——直接欠薪(余额不变、treasury_credit 零调用)。"""
    public = _resident("postman", "postman")
    db_session.add(public)
    await db_session.commit()

    monkeypatch.setattr(settings, "npc_economy_enabled", True)
    monkeypatch.setattr(settings, "town_duty_funding_enabled", True)
    monkeypatch.setattr(settings, "town_treasury_enabled", False)
    monkeypatch.setattr(settings, "town_public_duty_wage_sc", 1)

    credit_calls: list[tuple] = []
    real_credit = coin_service.treasury_credit

    async def spy_credit(*args, **kwargs):
        credit_calls.append((args, kwargs))
        return await real_credit(*args, **kwargs)

    monkeypatch.setattr(coin_service, "treasury_credit", spy_credit)

    await duty_service._pay_wage(db_session, public)

    assert credit_calls == []
    assert await coin_service.treasury_balance(db_session, "postman") == 0


@pytest.mark.anyio
async def test_sustainable_gate_never_falls_back_to_mint(db_session, monkeypatch):
    public = _resident("postman", "postman")
    db_session.add(public)
    await db_session.commit()

    monkeypatch.setattr(settings, "npc_economy_enabled", True)
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_ledger_enabled", True)
    monkeypatch.setattr(settings, "town_duty_funding_enabled", True)
    monkeypatch.setattr(settings, "town_wage_unfunded_policy", "mint")
    monkeypatch.setattr(settings, "town_public_duty_wage_sc", 1)
    # No realized income: budget rejects the wage.  The old emergency setting
    # may remain in an ops env, but opening the new gate must still not mint.
    await duty_service._pay_wage(db_session, public)
    assert await coin_service.treasury_balance(db_session, "postman") == 0
