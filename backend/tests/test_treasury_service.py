"""S1-5 镇财政闭环 — TreasuryService / 税 hook / funded wage / nightly / REST-WS.

Spec: archive/2026-07-25/docs/kickoffs/KICKOFF_S1-5_treasury.md §5 (test names are
taken verbatim from that section).

Every gated path carries a "gate off → byte-level status quo" assertion: the
module's single master switch is ``settings.town_treasury_enabled`` and it
defaults to False, so the whole suite must pin it explicitly.
"""
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.config import settings
from app.models.town_treasury import TOWN_KEY, TownTreasury
from app.services import treasury_service


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------------------------- #
# Task 1 — town_treasuries table + model + migration                          #
# --------------------------------------------------------------------------- #

def test_town_treasury_migration_single_head():
    """`alembic heads` stays single-headed in this worktree and the S1-5
    migration chains onto the measured head (047_add_issue_stances).

    NOTE (收口): the migration file keeps the ``NNN`` placeholder number — the
    parallel S2-5 line also chains onto 047, so the main session linearizes the
    numbers at merge time and re-runs this single-head assertion.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic multi-head: {heads}"
    rev = script.get_revision("NNN_add_town_treasury")
    assert rev is not None
    assert rev.down_revision == "047_add_issue_stances"


def test_town_treasury_model_shape():
    """Mirrors resident_treasuries: slug-ish PK + balance_sc + updated_at."""
    cols = TownTreasury.__table__.columns
    assert TownTreasury.__tablename__ == "town_treasuries"
    assert cols["key"].primary_key is True
    assert isinstance(cols["key"].type, sa.String)
    assert cols["key"].type.length == 100
    assert isinstance(cols["balance_sc"].type, sa.Integer)
    assert isinstance(cols["updated_at"].type, sa.DateTime)
    assert cols["updated_at"].type.timezone is True
    assert TOWN_KEY == "town"


@pytest.mark.anyio
async def test_town_treasuries_table_created(db_engine):
    """models/__init__.py registers the model so Base.metadata.create_all
    (the main.py / conftest test path) sees the new table."""
    async with db_engine.connect() as conn:
        names = await conn.run_sync(lambda sc: sa.inspect(sc).get_table_names())
    assert "town_treasuries" in names


@pytest.mark.anyio
async def test_town_treasury_starts_empty(db_session):
    """The town account is created on demand (upsert), not seeded."""
    rows = (await db_session.execute(select(TownTreasury))).scalars().all()
    assert rows == []


# --------------------------------------------------------------------------- #
# Task 2 — TreasuryService.tax / disburse / balance                            #
# --------------------------------------------------------------------------- #

async def _row(db):
    return (await db.execute(
        select(TownTreasury).execution_options(populate_existing=True)
    )).scalars().first()


@pytest.mark.anyio
async def test_tax_credits_town_balance(db_session):
    await treasury_service.tax(db_session, 100, reason="sales_tax:x")
    assert await treasury_service.balance(db_session) == 100
    # the account row is upserted on demand
    row = await _row(db_session)
    assert row is not None and row.key == TOWN_KEY and row.balance_sc == 100
    # a second credit accumulates on the same row (no duplicate insert)
    await treasury_service.tax(db_session, 5, reason="sales_tax:y")
    assert await treasury_service.balance(db_session) == 105
    assert len((await db_session.execute(select(TownTreasury))).scalars().all()) == 1


@pytest.mark.anyio
async def test_tax_amount_zero_is_noop(db_session):
    """coin_service's ``amount <= 0`` guard is preserved verbatim: no balance
    change and — critically — no row is created."""
    await treasury_service.tax(db_session, 0, reason="zero")
    await treasury_service.tax(db_session, -5, reason="negative")
    assert await treasury_service.balance(db_session) == 0
    assert (await _row(db_session)) is None


@pytest.mark.anyio
async def test_disburse_guarded_decrement(db_session):
    await treasury_service.tax(db_session, 100, reason="seed")
    assert await treasury_service.disburse(db_session, 30, reason="wage:ann") is True
    assert await treasury_service.balance(db_session) == 70


@pytest.mark.anyio
async def test_disburse_insufficient_returns_false_no_exception(db_session):
    await treasury_service.tax(db_session, 10, reason="seed")
    assert await treasury_service.disburse(db_session, 50, reason="wage:ann") is False
    assert await treasury_service.balance(db_session) == 10


@pytest.mark.anyio
async def test_disburse_missing_account_returns_false(db_session):
    """No town row at all → the guard matches 0 rows → False, no row created."""
    assert await treasury_service.disburse(db_session, 1, reason="x") is False
    assert (await _row(db_session)) is None


@pytest.mark.anyio
async def test_disburse_amount_zero_is_noop_false(db_session):
    await treasury_service.tax(db_session, 10, reason="seed")
    assert await treasury_service.disburse(db_session, 0, reason="zero") is False
    assert await treasury_service.disburse(db_session, -3, reason="neg") is False
    assert await treasury_service.balance(db_session) == 10


@pytest.mark.anyio
async def test_concurrent_tax_no_lost_update(db_engine):
    """Two independent sessions crediting the same town row must sum, not clobber.

    A read-modify-write implementation loses one of the two credits here (both
    sessions read the same pre-image); the guarded UPDATE / ON CONFLICT DO UPDATE
    upsert survives it.
    """
    import asyncio
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)

    async def credit(amount: int) -> None:
        async with maker() as db:
            await treasury_service.tax(db, amount, reason="concurrent")

    # first credit creates the row; the racing pair then both hit the conflict path
    await credit(10)
    await asyncio.gather(*(credit(7) for _ in range(5)))
    async with maker() as db:
        assert await treasury_service.balance(db) == 10 + 7 * 5


@pytest.mark.anyio
async def test_concurrent_disburse_no_overspend(db_engine):
    """The ``balance_sc >= amount`` guard means concurrent spenders can never
    drive the town account negative — the losers just get False."""
    import asyncio
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as db:
        await treasury_service.tax(db, 100, reason="seed")

    async def spend() -> bool:
        async with maker() as db:
            return await treasury_service.disburse(db, 30, reason="wage")

    results = await asyncio.gather(*(spend() for _ in range(6)))
    async with maker() as db:
        remaining = await treasury_service.balance(db)
    assert remaining >= 0
    assert sum(results) == 3          # 3 × 30 fits in 100, the 4th+ are refused
    assert remaining == 100 - 30 * sum(results)


@pytest.mark.anyio
async def test_no_rollback_on_zero_row_guard(db_session):
    """MissingGreenlet regression gate (coin_service.charge's comment): when the
    guard matches 0 rows nothing was written, so rollback() — which expires every
    ORM object in the caller's session — must NOT be called."""
    await treasury_service.tax(db_session, 10, reason="seed")
    calls = []
    original = db_session.rollback

    async def _spy():
        calls.append(1)
        await original()

    db_session.rollback = _spy
    try:
        assert await treasury_service.disburse(db_session, 999, reason="too much") is False
        assert await treasury_service.disburse(db_session, 0, reason="zero") is False
    finally:
        db_session.rollback = original
    assert calls == []


@pytest.mark.anyio
async def test_town_treasury_not_in_transactions_ledger(db_session):
    """transactions.user_id is a hard FK to users.id, so the synthetic town
    account cannot be a ledger row (deliberate deviation, model docstring)."""
    from app.models.transaction import Transaction

    await treasury_service.tax(db_session, 50, reason="sales_tax:x")
    await treasury_service.disburse(db_session, 20, reason="wage:ann")
    rows = (await db_session.execute(select(Transaction))).scalars().all()
    assert rows == []


# --------------------------------------------------------------------------- #
# Task 3 — 税收 hook 接线 (销售税主入口 + 送礼/打赏旋钮, 全部门控 + fail-open)    #
# --------------------------------------------------------------------------- #

def _work_item(creator_slug: str, price_sc: int = 20, stock: int = 3):
    from app.models.shop import Item
    return Item(
        code=f"work_x_{creator_slug}", kind="resident_work", name="手工件",
        description="", icon="🔧", price_sc=price_sc, active=True,
        payload_json={"creator_slug": creator_slug, "stock": stock},
    )


@pytest.mark.anyio
async def test_tax_hook_disabled_no_skim(db_session, monkeypatch):
    """Gate off → the maker receives the full amount and the town account is
    never even created (byte-level status quo)."""
    from app.models.resident import Resident
    from app.services import coin_service, shop_effects

    monkeypatch.setattr(settings, "town_treasury_enabled", False)
    monkeypatch.setattr(settings, "town_tax_rate_sales", 0.1)
    item = _work_item("ann", price_sc=20)
    db_session.add_all([
        item,
        Resident(slug="ann", name="安", district="workshop", status="idle",
                 resident_type="npc", tile_x=1, tile_y=1, meta_json={}),
    ])
    await db_session.commit()

    out = await shop_effects._resident_work_effect(db_session, "buyer", item, 1, {})
    assert out["earned"] == 20
    assert await coin_service.treasury_balance(db_session, "ann") == 20
    assert await treasury_service.balance(db_session) == 0
    assert (await _row(db_session)) is None


@pytest.mark.anyio
async def test_resident_work_sale_skims_sales_tax(db_session, monkeypatch):
    """Gate on → int(earned * rate) is skimmed into the town treasury and the
    maker receives the remainder. Conservation: cut + net == earned."""
    from app.models.resident import Resident
    from app.services import coin_service, shop_effects

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_tax_rate_sales", 0.1)
    item = _work_item("ann", price_sc=20)
    db_session.add_all([
        item,
        Resident(slug="ann", name="安", district="workshop", status="idle",
                 resident_type="npc", tile_x=1, tile_y=1, meta_json={}),
    ])
    await db_session.commit()

    out = await shop_effects._resident_work_effect(db_session, "buyer", item, 1, {})
    assert out["sales_tax"] == 2 and out["earned"] == 18
    assert await coin_service.treasury_balance(db_session, "ann") == 18
    assert await treasury_service.balance(db_session) == 2


@pytest.mark.anyio
async def test_sales_tax_rounds_down_and_never_starves_the_maker(db_session, monkeypatch):
    """int() truncation: a 1 SC sale at 10% skims 0 — the maker keeps it all."""
    from app.models.resident import Resident
    from app.services import coin_service, shop_effects

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_tax_rate_sales", 0.1)
    item = _work_item("bo", price_sc=1)
    db_session.add_all([
        item,
        Resident(slug="bo", name="波", district="workshop", status="idle",
                 resident_type="npc", tile_x=1, tile_y=1, meta_json={}),
    ])
    await db_session.commit()

    out = await shop_effects._resident_work_effect(db_session, "buyer", item, 1, {})
    assert out["sales_tax"] == 0 and out["earned"] == 1
    assert await coin_service.treasury_balance(db_session, "bo") == 1
    assert await treasury_service.balance(db_session) == 0


@pytest.mark.anyio
async def test_sales_tax_failure_is_fail_open(db_session, monkeypatch):
    """A treasury error must never break a purchase: the maker still gets the
    full amount (fail-open discipline shared by every economy hook)."""
    from app.models.resident import Resident
    from app.services import coin_service, shop_effects

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_tax_rate_sales", 0.5)

    async def _boom(*a, **kw):
        raise RuntimeError("treasury down")

    monkeypatch.setattr(treasury_service, "tax", _boom)
    item = _work_item("cai", price_sc=20)
    db_session.add_all([
        item,
        Resident(slug="cai", name="蔡", district="workshop", status="idle",
                 resident_type="npc", tile_x=1, tile_y=1, meta_json={}),
    ])
    await db_session.commit()

    out = await shop_effects._resident_work_effect(db_session, "buyer", item, 1, {})
    assert out["sales_tax"] == 0 and out["earned"] == 20
    assert await coin_service.treasury_balance(db_session, "cai") == 20


@pytest.mark.anyio
async def test_purchase_resident_work_skims_sales_tax(db_session, monkeypatch):
    """End-to-end through shop_service.purchase(): buyer charged in full, the
    town skims its cut, and the maker's write-through wallet cache reflects net."""
    from app.models.resident import Resident
    from app.models.user import User
    from app.services import coin_service, shop_service
    import app.services.shop_effects  # noqa: F401 — registers the handlers

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_tax_rate_sales", 0.1)
    # Pin the 集市日 discount: it rides a process-wide active-event cache that
    # other suites warm up, and a discounted total would confuse the tax math
    # below (the payout is computed off item.price_sc, not off the charged total
    # — pre-existing _resident_work_effect behavior, untouched by S1-5).
    async def _full_price(_db):
        return 1.0

    monkeypatch.setattr(shop_service, "_market_discount", _full_price)
    item = _work_item("ann", price_sc=20)
    maker = Resident(slug="ann", name="安", district="workshop", status="idle",
                     resident_type="npc", tile_x=1, tile_y=1, meta_json={})
    db_session.add_all([
        item, maker,
        User(id="buyer", name="Buyer", email="b@x.io", soul_coin_balance=100),
    ])
    await db_session.commit()

    res = await shop_service.purchase(db_session, "buyer", item.code, qty=1)
    assert res["ok"] and res["total_sc"] == 20
    assert await treasury_service.balance(db_session) == 2
    assert await coin_service.treasury_balance(db_session, "ann") == 18
    assert (maker.meta_json or {}).get("wallet") == 18


@pytest.mark.anyio
async def test_gift_tax_rate_zero_is_status_quo(db_session, monkeypatch):
    """town_tax_rate_gift defaults to 0 → the creator's 20% share is untouched
    even with the master gate ON (the knob exists, it just doesn't bite)."""
    from app.models.resident import Resident
    from app.models.shop import Item
    from app.models.user import User
    from app.services import shop_effects

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_tax_rate_gift", 0.0)
    item = Item(code="flower", kind="gift", name="花", price_sc=50, active=True,
                payload_json={"relationship_boost": 0.1})
    db_session.add_all([
        item,
        Resident(slug="dee", name="丁", district="workshop", status="idle",
                 resident_type="npc", tile_x=1, tile_y=1, creator_id="creator"),
        User(id="creator", name="C", email="c@x.io", soul_coin_balance=0),
        User(id="buyer", name="B", email="b2@x.io", soul_coin_balance=100),
    ])
    await db_session.commit()

    out = await shop_effects._gift_effect(
        db_session, "buyer", item, 1, {"resident_slug": "dee"})
    assert out["creator_share"] == 10          # 20% of 50, unchanged
    assert out["gift_tax"] == 0
    assert await treasury_service.balance(db_session) == 0


@pytest.mark.anyio
async def test_gift_tax_rate_nonzero_skims_creator_share(db_session, monkeypatch):
    from app.models.resident import Resident
    from app.models.shop import Item
    from app.models.user import User
    from app.services import shop_effects

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_tax_rate_gift", 0.2)
    item = Item(code="flower", kind="gift", name="花", price_sc=50, active=True,
                payload_json={"relationship_boost": 0.1})
    db_session.add_all([
        item,
        Resident(slug="dee", name="丁", district="workshop", status="idle",
                 resident_type="npc", tile_x=1, tile_y=1, creator_id="creator"),
        User(id="creator", name="C", email="c@x.io", soul_coin_balance=0),
        User(id="buyer", name="B", email="b2@x.io", soul_coin_balance=100),
    ])
    await db_session.commit()

    out = await shop_effects._gift_effect(
        db_session, "buyer", item, 1, {"resident_slug": "dee"})
    assert out["creator_share"] == 8 and out["gift_tax"] == 2   # 10 → 8 + 2
    assert await treasury_service.balance(db_session) == 2


@pytest.mark.anyio
async def test_tip_tax_rate_zero_is_status_quo(db_session, monkeypatch):
    from app.models.bulletin_post import BulletinPost
    from app.models.resident import Resident
    from app.models.shop import Item
    from app.models.user import User
    from app.services import shop_effects

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_tax_rate_gift", 0.0)
    r = Resident(slug="edd", name="鄂", district="workshop", status="idle",
                 resident_type="npc", tile_x=1, tile_y=1, creator_id="creator")
    db_session.add_all([
        r,
        Item(code="tip5", kind="tip", name="打赏", price_sc=5, active=True),
        User(id="creator", name="C", email="c3@x.io", soul_coin_balance=0),
        User(id="buyer", name="B", email="b3@x.io", soul_coin_balance=100),
    ])
    await db_session.commit()
    post = BulletinPost(kind="notice", title="t", content_md="c",
                        author_resident_id=r.id)
    db_session.add(post)
    await db_session.commit()

    tip_item = (await db_session.execute(
        select(Item).where(Item.code == "tip5"))).scalars().one()
    out = await shop_effects._tip_effect(
        db_session, "buyer", tip_item, 1, {"post_id": post.id})
    assert out["creator_share"] == 4 and out["tip_tax"] == 0
    assert await treasury_service.balance(db_session) == 0


# --------------------------------------------------------------------------- #
# Task 4 — funded wage: _pay_wage draws from the town treasury                 #
# --------------------------------------------------------------------------- #

def _npc(slug: str, name: str, duty: dict | None = None, **kw):
    from app.models.resident import Resident
    meta = {"duty": duty} if duty else {}
    d = dict(slug=slug, name=name, district="workshop", status="idle",
             resident_type="npc", tile_x=116, tile_y=27, meta_json=meta)
    d.update(kw)
    return Resident(**d)


@pytest.mark.anyio
async def test_pay_wage_disabled_mints_as_before(db_session, monkeypatch):
    """Gate off → the pre-S1-5 behavior verbatim: the wage is MINTED into the
    resident's purse and the town account is not touched (not even created)."""
    from app.services import coin_service, duty_service

    monkeypatch.setattr(settings, "town_treasury_enabled", False)
    r = _npc("chen", "陈铁生", {"key": "workshop_fixer", "perks": {"wage_sc": 8}})
    db_session.add(r)
    await db_session.commit()

    await duty_service._pay_wage(db_session, r)
    assert await coin_service.treasury_balance(db_session, "chen") == 8
    assert (r.meta_json or {}).get("wallet") == 8
    assert await treasury_service.balance(db_session) == 0
    assert (await _row(db_session)) is None


@pytest.mark.anyio
async def test_pay_wage_enabled_draws_from_town(db_session, monkeypatch):
    from app.services import coin_service, duty_service

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "election_enabled", False)
    await treasury_service.tax(db_session, 100, reason="seed")
    r = _npc("chen", "陈铁生", {"key": "workshop_fixer", "perks": {"wage_sc": 8}})
    db_session.add(r)
    await db_session.commit()

    await duty_service._pay_wage(db_session, r)
    assert await treasury_service.balance(db_session) == 92
    assert await coin_service.treasury_balance(db_session, "chen") == 8
    assert (r.meta_json or {}).get("wallet") == 8


@pytest.mark.anyio
async def test_funded_wage_conserves_total(db_session, monkeypatch):
    """Money moves between accounts instead of being created: town + residents
    is invariant across a funded wage (the MINT path deliberately is not)."""
    from app.services import coin_service, duty_service

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "election_enabled", False)
    await treasury_service.tax(db_session, 100, reason="seed")
    r = _npc("chen", "陈铁生", {"key": "workshop_fixer", "perks": {"wage_sc": 8}})
    db_session.add(r)
    await db_session.commit()

    before = (await treasury_service.balance(db_session)
              + await coin_service.treasury_balance(db_session, "chen"))
    await duty_service._pay_wage(db_session, r)
    after = (await treasury_service.balance(db_session)
             + await coin_service.treasury_balance(db_session, "chen"))
    assert before == after == 100


@pytest.mark.anyio
async def test_pay_wage_unfunded_policy_skip(db_session, monkeypatch):
    """Town treasury empty + policy 'skip' → 欠薪: nothing is credited and, above
    all, nothing is minted."""
    from app.services import coin_service, duty_service

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_wage_unfunded_policy", "skip")
    monkeypatch.setattr(settings, "election_enabled", False)
    await treasury_service.tax(db_session, 3, reason="seed")   # < wage
    r = _npc("chen", "陈铁生", {"key": "workshop_fixer", "perks": {"wage_sc": 8}})
    db_session.add(r)
    await db_session.commit()

    await duty_service._pay_wage(db_session, r)
    assert await coin_service.treasury_balance(db_session, "chen") == 0
    assert await treasury_service.balance(db_session) == 3


@pytest.mark.anyio
async def test_pay_wage_unfunded_policy_mint(db_session, monkeypatch):
    """policy 'mint' → escape hatch back to the pre-S1-5 mint-from-nothing path
    (the town balance is untouched, the resident still gets paid)."""
    from app.services import coin_service, duty_service

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_wage_unfunded_policy", "mint")
    monkeypatch.setattr(settings, "election_enabled", False)
    await treasury_service.tax(db_session, 3, reason="seed")
    r = _npc("chen", "陈铁生", {"key": "workshop_fixer", "perks": {"wage_sc": 8}})
    db_session.add(r)
    await db_session.commit()

    await duty_service._pay_wage(db_session, r)
    assert await coin_service.treasury_balance(db_session, "chen") == 8
    assert await treasury_service.balance(db_session) == 3


@pytest.mark.anyio
async def test_mayor_bonus_funded_from_town(db_session, monkeypatch):
    """S2-1 regression gate: the mayor bonus semantics (meta_json['mayor'] ×
    election_mayor_wage_bonus) are untouched — S1-5 only changes WHO pays, so the
    town funds the bonused amount too."""
    from app.services import coin_service, duty_service

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "election_enabled", True)
    monkeypatch.setattr(settings, "election_mayor_wage_bonus", 1.5)
    await treasury_service.tax(db_session, 100, reason="seed")
    r = _npc("mayor", "镇长", {"key": "workshop_fixer", "perks": {"wage_sc": 8}})
    r.meta_json = {**(r.meta_json or {}), "mayor": True}
    db_session.add(r)
    await db_session.commit()

    await duty_service._pay_wage(db_session, r)
    assert await coin_service.treasury_balance(db_session, "mayor") == 12   # 8 × 1.5
    assert await treasury_service.balance(db_session) == 88


@pytest.mark.anyio
async def test_pay_wage_treasury_error_is_fail_open(db_session, monkeypatch):
    """A treasury failure must never break the tick: _pay_wage swallows it."""
    from app.services import coin_service, duty_service

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "election_enabled", False)

    async def _boom(*a, **kw):
        raise RuntimeError("treasury down")

    monkeypatch.setattr(treasury_service, "disburse", _boom)
    r = _npc("chen", "陈铁生", {"key": "workshop_fixer", "perks": {"wage_sc": 8}})
    db_session.add(r)
    await db_session.commit()

    await duty_service._pay_wage(db_session, r)          # must not raise
    assert await coin_service.treasury_balance(db_session, "chen") == 0


# --------------------------------------------------------------------------- #
# Task 5 — nightly public spending job                                         #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_nightly_public_spending_seeded(db_session, monkeypatch):
    """Seeded town account + a daily works budget → the disbursed amount equals
    the balance delta exactly (拨款守恒), and the run is stamped in ConfigService."""
    from app.services.config_service import ConfigService

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_public_works_daily_sc", 30)
    await treasury_service.tax(db_session, 100, reason="seed")

    before = await treasury_service.balance(db_session)
    spent = await treasury_service.run_public_spending(db_session)
    after = await treasury_service.balance(db_session)
    assert spent == 30
    assert before - after == spent
    stamp = await ConfigService(db_session).get(treasury_service.LAST_SPEND_KEY)
    assert isinstance(stamp, str) and stamp


@pytest.mark.anyio
async def test_public_spending_capped_by_balance(db_session, monkeypatch):
    """The town cannot deficit-spend: the budget is clamped to the balance and
    the guarded decrement never drives it negative."""
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_public_works_daily_sc", 30)
    await treasury_service.tax(db_session, 12, reason="seed")

    spent = await treasury_service.run_public_spending(db_session)
    assert spent == 12
    assert await treasury_service.balance(db_session) == 0


@pytest.mark.anyio
async def test_public_spending_zero_budget_only_reconciles(db_session, monkeypatch):
    """Default budget 0 → reconcile-only: the stamp is written, no coins move."""
    from app.services.config_service import ConfigService

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_public_works_daily_sc", 0)
    await treasury_service.tax(db_session, 50, reason="seed")

    assert await treasury_service.run_public_spending(db_session) == 0
    assert await treasury_service.balance(db_session) == 50
    assert await ConfigService(db_session).get(treasury_service.LAST_SPEND_KEY)


@pytest.mark.anyio
async def test_public_spending_skipped_when_disabled(db_session, monkeypatch):
    """Gate off → the job is a whole no-op: no spend, no ConfigService stamp."""
    from app.services.config_service import ConfigService

    monkeypatch.setattr(settings, "town_treasury_enabled", False)
    monkeypatch.setattr(settings, "town_public_works_daily_sc", 30)
    await treasury_service.tax(db_session, 100, reason="seed")

    assert await treasury_service.run_public_spending(db_session) == 0
    assert await treasury_service.balance(db_session) == 100
    assert await ConfigService(db_session).get(treasury_service.LAST_SPEND_KEY) is None


def test_nightly_public_spending_wired_and_gated():
    """The cron must carry an isolated, gated block (test_m5_space wiring-guard
    pattern): gate INSIDE the cron, own try/except, fail-open, appended after the
    existing governance blocks without moving any of them."""
    import inspect
    from app.tasks import nightly_cron

    src = inspect.getsource(nightly_cron.run_nightly_jobs)
    assert "town_treasury_enabled" in src
    assert "run_public_spending" in src
    # gate is checked before the service is imported/called → skip = zero DB touch
    assert src.index("town_treasury_enabled") < src.index("run_public_spending")
    # appended AFTER the existing governance blocks (they must not be moved)
    assert src.index("close_due_polls") < src.index("run_public_spending")
    assert src.index("term_check") < src.index("run_public_spending")


@pytest.mark.anyio
async def test_nightly_block_runs_public_spending(db_engine, monkeypatch):
    """Functional: the cron block body disburses through the shared session
    factory when the gate is on."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_public_works_daily_sc", 5)
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        await treasury_service.tax(db, 20, reason="seed")

    # run only the S1-5 block body (not the whole cron — LLM-adjacent jobs)
    async with factory() as db:
        spent = await treasury_service.run_public_spending(db)
    assert spent == 5
    async with factory() as db:
        assert await treasury_service.balance(db) == 15


# --------------------------------------------------------------------------- #
# Task 6 — read-only REST endpoint + treasury_changed WS event                 #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_get_town_treasury_readonly_auth(client, db_session, monkeypatch):
    """Player read-only endpoint: a logged-in (non-admin) user may read it; an
    anonymous or invalid caller gets 401. It never exposes a write verb."""
    from app.models.user import User
    from app.services.auth_service import create_token

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_tax_rate_sales", 0.1)
    await treasury_service.tax(db_session, 42, reason="seed")

    pleb = User(name="pleb", email="pleb-treasury@test.com", is_admin=False, is_banned=False)
    db_session.add(pleb)
    await db_session.commit()

    assert (await client.get("/townhall/treasury")).status_code == 401
    bad = await client.get("/townhall/treasury",
                           headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401

    ok = await client.get("/townhall/treasury",
                          headers={"Authorization": f"Bearer {create_token(pleb.id)}"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["balance_sc"] == 42
    assert body["tax_rate"] == 0.1
    assert body["enabled"] is True
    assert isinstance(body["updated_at"], str)
    # read-only: no write verb is mounted on this path
    assert (await client.post("/townhall/treasury", json={})).status_code in (404, 405)


@pytest.mark.anyio
async def test_get_town_treasury_when_disabled(client, db_session, monkeypatch):
    """Gate off → the projection still reads (it is a pure read) but reports a
    zero balance and enabled=False; no town row is created by the read."""
    from app.models.user import User
    from app.services.auth_service import create_token

    monkeypatch.setattr(settings, "town_treasury_enabled", False)
    pleb = User(name="pleb", email="pleb-treasury2@test.com", is_admin=False, is_banned=False)
    db_session.add(pleb)
    await db_session.commit()

    res = await client.get("/townhall/treasury",
                           headers={"Authorization": f"Bearer {create_token(pleb.id)}"})
    assert res.status_code == 200
    assert res.json()["balance_sc"] == 0 and res.json()["enabled"] is False
    assert (await _row(db_session)) is None


@pytest.mark.anyio
async def test_treasury_changed_ws_envelope_has_seq_revision(db_session, monkeypatch):
    """A significant balance move broadcasts a treasury_changed envelope carrying
    the world revision / seq anchors (world_changed v1 shape, seq reuses the
    OutboxEvent cursor — no new counter)."""
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_ws_min_delta_sc", 10)

    sent: list[dict] = []

    async def _capture(payload=None):
        sent.append(payload)

    import app.lab.apply as apply_engine
    monkeypatch.setattr(apply_engine, "broadcast_world_changed", _capture)

    await treasury_service.notify_changed(db_session, delta=50, reason="sales_tax:x")
    assert len(sent) == 1
    env = sent[0]
    assert env["type"] == "treasury_changed"
    assert "seq" in env and "world_revision_id" in env
    assert env["schema_version"] == 1
    assert env["delta_sc"] == 50 and env["reason"] == "sales_tax:x"
    assert isinstance(env["occurred_at"], str) and env["event_id"]


@pytest.mark.anyio
async def test_treasury_changed_below_threshold_is_silent(db_session, monkeypatch):
    """High-frequency micro-skims must not spam the WS channel; the default
    threshold 0 disables broadcasting outright."""
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    sent: list[dict] = []

    async def _capture(payload=None):
        sent.append(payload)

    import app.lab.apply as apply_engine
    monkeypatch.setattr(apply_engine, "broadcast_world_changed", _capture)

    monkeypatch.setattr(settings, "town_ws_min_delta_sc", 10)
    await treasury_service.notify_changed(db_session, delta=9, reason="tiny")
    monkeypatch.setattr(settings, "town_ws_min_delta_sc", 0)
    await treasury_service.notify_changed(db_session, delta=9999, reason="huge")
    monkeypatch.setattr(settings, "town_treasury_enabled", False)
    monkeypatch.setattr(settings, "town_ws_min_delta_sc", 1)
    await treasury_service.notify_changed(db_session, delta=9999, reason="gate off")
    assert sent == []


@pytest.mark.anyio
async def test_nightly_public_spending_broadcasts(db_session, monkeypatch):
    """The nightly job is the intended low-frequency broadcast trigger."""
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "town_public_works_daily_sc", 30)
    monkeypatch.setattr(settings, "town_ws_min_delta_sc", 10)
    await treasury_service.tax(db_session, 100, reason="seed")

    sent: list[dict] = []

    async def _capture(payload=None):
        sent.append(payload)

    import app.lab.apply as apply_engine
    monkeypatch.setattr(apply_engine, "broadcast_world_changed", _capture)

    assert await treasury_service.run_public_spending(db_session) == 30
    assert [e["type"] for e in sent] == ["treasury_changed"]
    assert sent[0]["delta_sc"] == -30


@pytest.mark.anyio
async def test_treasury_numbers_never_enter_npc_prompt(db_session, monkeypatch):
    """§7 hard rule: town-wide finance figures must never reach an NPC prompt.

    The decision prompt may only see the resident's OWN wallet cache — the town
    balance, tax rate and public-works budget are absent from the rendered text.
    """
    from app.agent.prompts import build_decision_prompt
    from app.agent.actions import ActionType

    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    await treasury_service.tax(db_session, 4242, reason="seed")

    r = _npc("chen", "陈铁生", {"key": "workshop_fixer",
                               "prompt_hint": "你在工坊修东西", "perks": {}})
    r.meta_json = {**(r.meta_json or {}), "wallet": 7}
    system, user = build_decision_prompt(
        r, "工作时段", "10:00", [], [], [], [ActionType.WORK], 20,
    )
    blob = f"{system}\n{user}"
    assert "4242" not in blob
    assert "town_treasury" not in blob and "镇财政" not in blob
    assert "tax" not in blob.lower()
