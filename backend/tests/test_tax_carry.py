"""M-A C5 — skim_tax 两态 API + 分数税账(独立闸 TAX_CARRY_ENABLED)。

Plan: `docs/plans/2026-08-09-M-A-npc-economy.md` Step 3;spec §4 C5。

三条硬线:
1. `town_treasury_enabled` 关 → 两版都返 0、零写入(现状逐字节)。
2. `tax_carry_enabled` 关(vm212 在产前提:主闸已开)→ 逐字节等价旧 `int()` 截断,
   不碰 carry 行——"暗上"这一步不能改在产玩家路径的一分钱。
3. 双闸开 → 整数部分即征、尾数累进 `system_config.town_tax_carry`。

断言**一律新开 session 重读**:conftest 的 `:memory:` 引擎走 StaticPool(所有
session 共用一条连接,读得到别人尚未 commit 的改动,pending 原语会假绿),所以本
模块自建文件型 sqlite——"没 commit 就看不见"才立得住,pending/自提交两版的差别
才测得出来。
"""
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base
from app.models.system_config import SystemConfig
from app.models.town_treasury import TownTreasury
from app.services import treasury_service

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def sessions(tmp_path):
    """Session factory on a file-backed sqlite — real per-session connections."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tax.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def treasury_on(monkeypatch):
    """主闸开、政策存储关(走 fallback rate)——carry 闸由各用例自己拍。"""
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    monkeypatch.setattr(settings, "polis_policy_enabled", False)
    return settings


async def _town(sessions) -> int:
    """新 session 重读镇库 —— 只承认已落库的钱。"""
    async with sessions() as db:
        return await treasury_service.balance(db)


async def _carry_row(sessions) -> SystemConfig | None:
    async with sessions() as db:
        return (await db.execute(
            select(SystemConfig).where(SystemConfig.key == treasury_service.TAX_CARRY_KEY)
        )).scalar_one_or_none()


async def _carry(sessions) -> float:
    row = await _carry_row(sessions)
    return 0.0 if row is None else float(row.value)


# --------------------------------------------------------------------------- #
# 1. 主闸关 → 两版零写入                                                        #
# --------------------------------------------------------------------------- #

async def test_gate_off_both_apis_return_zero_and_write_nothing(sessions, monkeypatch):
    monkeypatch.setattr(settings, "town_treasury_enabled", False)
    monkeypatch.setattr(settings, "tax_carry_enabled", True)

    async with sessions() as db:
        assert await treasury_service.skim_tax_pending(db, 100, 0.05, "sales_tax:x") == 0
        assert await treasury_service.skim_tax(db, 100, 0.05, "sales_tax:x") == 0
        await db.commit()

    assert await _town(sessions) == 0
    assert await _carry_row(sessions) is None
    async with sessions() as db:
        assert (await db.execute(select(TownTreasury))).scalars().all() == []


# --------------------------------------------------------------------------- #
# 2. carry 闸关 → 逐字节旧 int() 截断                                           #
# --------------------------------------------------------------------------- #

async def test_carry_off_truncates_like_the_legacy_int_cut(sessions, treasury_on, monkeypatch):
    """vm212 安全线:主闸已在产开着,carry 闸不开就必须一分不差还是旧行为。"""
    monkeypatch.setattr(settings, "tax_carry_enabled", False)

    async with sessions() as db:
        # 16 × 0.05 = 0.8 → 旧 int() 截断成 0,且不留任何尾数账。
        assert await treasury_service.skim_tax(db, 16, 0.05, "sales_tax:a") == 0
    assert await _town(sessions) == 0
    assert await _carry_row(sessions) is None

    async with sessions() as db:
        assert await treasury_service.skim_tax(db, 100, 0.05, "sales_tax:b") == 5
    assert await _town(sessions) == 5
    assert await _carry_row(sessions) is None


async def test_carry_off_pending_version_also_leaves_no_carry_row(sessions, treasury_on, monkeypatch):
    monkeypatch.setattr(settings, "tax_carry_enabled", False)

    async with sessions() as db:
        assert await treasury_service.skim_tax_pending(db, 100, 0.05, "sales_tax:c") == 5
        await db.commit()

    assert await _town(sessions) == 5
    assert await _carry_row(sessions) is None


# --------------------------------------------------------------------------- #
# 3. 双闸开 → 尾数累计                                                          #
# --------------------------------------------------------------------------- #

async def test_carry_on_accrues_the_fraction_until_it_pays_out(sessions, treasury_on, monkeypatch):
    """rate 0.05、gross 16 → 每笔 exact 0.8:首笔仍征 0,但尾数记账不再蒸发。"""
    monkeypatch.setattr(settings, "tax_carry_enabled", True)

    async with sessions() as db:
        assert await treasury_service.skim_tax(db, 16, 0.05, "sales_tax:a") == 0
    assert await _town(sessions) == 0
    assert await _carry(sessions) == pytest.approx(0.8)

    # 13 笔累计 exact = 10.4 → 镇库拿走 10,账上留 0.4。
    for _ in range(12):
        async with sessions() as db:
            await treasury_service.skim_tax(db, 16, 0.05, "sales_tax:a")

    assert await _town(sessions) == 10
    assert await _carry(sessions) == pytest.approx(0.4)


async def test_carry_row_carries_group_and_updated_at(sessions, treasury_on, monkeypatch):
    """`SystemConfig.group` 非 Optional 无默认 —— 漏了它 create_all 建表下直接炸。"""
    monkeypatch.setattr(settings, "tax_carry_enabled", True)

    async with sessions() as db:
        await treasury_service.skim_tax(db, 16, 0.05, "sales_tax:a")

    row = await _carry_row(sessions)
    assert row is not None
    assert row.group == "town"
    assert row.updated_at is not None
    assert row.updated_by
    # ConfigService.get 会 json.loads 这个值,存法必须是合法 JSON 数字。
    async with sessions() as db:
        from app.services.config_service import ConfigService
        assert await ConfigService(db).get(treasury_service.TAX_CARRY_KEY) == pytest.approx(0.8)


async def test_carry_upsert_updates_the_existing_row_in_place(sessions, treasury_on, monkeypatch):
    monkeypatch.setattr(settings, "tax_carry_enabled", True)

    for _ in range(2):
        async with sessions() as db:
            await treasury_service.skim_tax(db, 16, 0.05, "sales_tax:a")

    async with sessions() as db:
        rows = (await db.execute(select(SystemConfig).where(
            SystemConfig.key == treasury_service.TAX_CARRY_KEY))).scalars().all()
    assert len(rows) == 1
    assert await _town(sessions) == 1
    assert await _carry(sessions) == pytest.approx(0.6)


# --------------------------------------------------------------------------- #
# 4/5. 两态语义:pending flush-owned vs 自提交                                   #
# --------------------------------------------------------------------------- #

async def test_skim_tax_pending_is_flush_owned(sessions, treasury_on, monkeypatch):
    """调用后 rollback → 镇库与 carry 都不许留痕。"""
    monkeypatch.setattr(settings, "tax_carry_enabled", True)

    async with sessions() as db:
        assert await treasury_service.skim_tax_pending(db, 100, 0.05, "sales_tax:a") == 5
        assert await _town(sessions) == 0  # 另一条连接看不到未 commit 的税
        await db.rollback()

    assert await _town(sessions) == 0
    assert await _carry_row(sessions) is None


async def test_skim_tax_commits_on_its_own(sessions, treasury_on, monkeypatch):
    """自提交版:session 用完直接丢弃,税和尾数账都得已经在库里。"""
    monkeypatch.setattr(settings, "tax_carry_enabled", True)

    async with sessions() as db:
        assert await treasury_service.skim_tax(db, 100, 0.05, "sales_tax:a") == 5
        assert await _town(sessions) == 5  # 已 commit,另一条连接立刻看得到

    assert await _town(sessions) == 5
    assert await _carry(sessions) == pytest.approx(0.0)


async def test_skim_tax_commits_a_cut_of_zero_when_only_carry_moved(sessions, treasury_on, monkeypatch):
    """cut==0 但尾数进了账 —— 这一笔也必须落库,否则 carry 静默丢失。"""
    monkeypatch.setattr(settings, "tax_carry_enabled", True)

    async with sessions() as db:
        assert await treasury_service.skim_tax(db, 16, 0.05, "sales_tax:a") == 0

    assert await _carry(sessions) == pytest.approx(0.8)


async def test_skim_tax_does_not_commit_when_nothing_was_written(sessions, treasury_on, monkeypatch):
    """carry 闸关且 cut==0 → 旧路径根本不 commit,不许替调用方提交半截事务。"""
    monkeypatch.setattr(settings, "tax_carry_enabled", False)

    async with sessions() as db:
        db.add(TownTreasury(key="dangling", balance_sc=7))
        assert await treasury_service.skim_tax(db, 16, 0.05, "sales_tax:a") == 0

    async with sessions() as db:
        assert (await db.execute(select(TownTreasury).where(
            TownTreasury.key == "dangling"))).scalar_one_or_none() is None


# --------------------------------------------------------------------------- #
# 5b. shop_effects 薄委托:玩家路径的自提交语义必须保住                          #
# --------------------------------------------------------------------------- #

async def test_skim_town_tax_delegates_to_the_self_committing_version(sessions, treasury_on, monkeypatch):
    from app.services import shop_effects

    monkeypatch.setattr(settings, "tax_carry_enabled", False)

    async with sessions() as db:
        assert await shop_effects._skim_town_tax(db, 100, 0.05, "sales_tax:a") == 5
        # 委托的是自提交版:同一 session 还没 commit,另一条连接就该看到税。
        assert await _town(sessions) == 5


async def test_skim_town_tax_keeps_gate_off_status_quo(sessions, monkeypatch):
    from app.services import shop_effects

    monkeypatch.setattr(settings, "town_treasury_enabled", False)

    async with sessions() as db:
        assert await shop_effects._skim_town_tax(db, 100, 0.05, "sales_tax:a") == 0
    assert await _town(sessions) == 0


async def test_skim_town_tax_stays_fail_open(sessions, treasury_on, monkeypatch):
    """税写失败绝不能炸掉一次购买 —— 薄委托后 fail-open 纪律不许丢。"""
    from app.services import shop_effects

    async def boom(*args, **kwargs):
        raise RuntimeError("treasury exploded")

    monkeypatch.setattr(treasury_service, "skim_tax", boom)

    async with sessions() as db:
        assert await shop_effects._skim_town_tax(db, 100, 0.05, "sales_tax:a") == 0


async def test_tip_path_sentinel_creator_still_lands_the_tax(sessions, treasury_on, monkeypatch):
    """回归 shop_effects.py:310-316 —— tip 路径在哨兵 creator 分支后没有任何保证
    到达的 commit,薄委托一旦改成 pending 版,这笔税就静默丢失。"""
    from app.models.bulletin_post import BulletinPost
    from app.models.resident import Resident
    from app.models.shop import Item
    from app.models.user import User
    from app.services import shop_effects
    from app.services.system_users import SYSTEM_CREATOR_ID

    monkeypatch.setattr(settings, "tax_carry_enabled", False)
    monkeypatch.setattr(settings, "town_tax_rate_gift", 0.2)

    async with sessions() as db:
        buyer = User(name="u", email="tip-carry-buyer@d2.com", soul_coin_balance=200)
        db.add_all([
            buyer,
            User(id=SYSTEM_CREATOR_ID, name="System",
                 email="system-tip-carry@d2.com", soul_coin_balance=0),
        ])
        await db.commit()
        resident = Resident(slug="sentinel-tip-carry", name="小明",
                            creator_id=SYSTEM_CREATOR_ID, district="central_plaza",
                            status="idle", tile_x=1, tile_y=1, persona_md="p")
        item = Item(code="tip_5sc_carry", kind="tip", name="打赏", price_sc=50,
                    active=True, payload_json={})
        db.add_all([resident, item])
        await db.commit()
        post = BulletinPost(kind="notice", title="t", content_md="c",
                            author_resident_id=resident.id)
        db.add(post)
        await db.commit()

        out = await shop_effects._tip_effect(db, buyer.id, item, 1, {"post_id": post.id})

    assert out["tip_tax"] == 8, "share=40 的 20% 必须照收"
    assert out["creator_share"] == 32, "哨兵 creator 不发钱,但税照收"
    assert await _town(sessions) == 8, "税必须已落库(自提交版),不能挂在 session 里"

    async with sessions() as db:
        sentinel = await db.get(User, SYSTEM_CREATOR_ID)
        assert sentinel.soul_coin_balance == 0


# --------------------------------------------------------------------------- #
# 6. 上界与政策覆盖                                                             #
# --------------------------------------------------------------------------- #

async def test_cut_is_capped_at_gross_when_carry_overflows(sessions, treasury_on, monkeypatch):
    """尾数账攒厚了也不能一次征走超过本笔毛额 —— cut = min(int(total), gross)。"""
    monkeypatch.setattr(settings, "tax_carry_enabled", True)

    async with sessions() as db:
        await treasury_service.kv_upsert_pending(
            db, treasury_service.TAX_CARRY_KEY, "5.000000", updated_by="test")
        await db.commit()

    async with sessions() as db:
        assert await treasury_service.skim_tax(db, 2, 0.5, "sales_tax:a") == 2

    assert await _town(sessions) == 2
    assert await _carry(sessions) == pytest.approx(4.0)


async def test_carry_off_cut_is_capped_at_gross(sessions, treasury_on, monkeypatch):
    monkeypatch.setattr(settings, "tax_carry_enabled", False)

    async with sessions() as db:
        assert await treasury_service.skim_tax(db, 3, 1.0, "sales_tax:a") == 3
    assert await _town(sessions) == 3


async def test_policy_tax_rate_overrides_the_fallback(sessions, treasury_on, monkeypatch):
    """镜像 test_fiscal_policy_wiring:政策表的 tax_rate 压过调用点的 fallback。"""
    from app.services.policy_service import PolicyService

    monkeypatch.setattr(settings, "polis_policy_enabled", True)
    monkeypatch.setattr(settings, "tax_carry_enabled", False)

    async with sessions() as db:
        svc = PolicyService(db)
        await svc.seed_defaults()
        assert await svc.apply_amend("tax_rate", 0.25, expected_version=1, updated_by="poll:1")

    async with sessions() as db:
        assert await treasury_service.skim_tax(db, 100, 0.03, "sales_tax:a") == 25

    assert await _town(sessions) == 25


async def test_kv_read_returns_default_when_missing(sessions):
    async with sessions() as db:
        assert await treasury_service.kv_read(db, "no_such_key", "0") == "0"
        await treasury_service.kv_upsert_pending(db, "some_key", "1.5", updated_by="test")
        await db.commit()
        assert await treasury_service.kv_read(db, "some_key", "0") == "1.5"
