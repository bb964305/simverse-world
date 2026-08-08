"""M-A C0 — 居民↔居民原子转账原语(coin_service 的 debit_pending / transfer)。

Plan: `docs/plans/2026-08-09-M-A-npc-economy.md` Step 2;spec §4 C0。

现状 `treasury_debit`(:494)与 `treasury_credit`(:484)各自带 commit,两段式拼
转账中途失败就烧钱。这里钉住的是替代品:一个 flush-owned 的 guarded debit,以及
debit-first、失败即回滚的原子 transfer。

断言**一律新开 session 重读**:conftest 的 `:memory:` 引擎走 StaticPool(所有
session 共用同一条连接,读得到别人尚未 commit 的改动,pending 原语会假绿),所以
本模块自建文件型 sqlite——一 session 一条真连接,"没 commit 就看不见"才立得住。
"""
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models.resident_treasury import ResidentTreasury
from app.services import coin_service
from app.services.coin_service import CoinError

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def sessions(tmp_path):
    """Session factory on a file-backed sqlite — real per-session connections."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'coin.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed(sessions, **balances: int) -> None:
    async with sessions() as db:
        for slug, amount in balances.items():
            db.add(ResidentTreasury(resident_slug=slug, balance_sc=amount))
        await db.commit()


async def _balance(sessions, slug: str) -> int:
    """新 session 重读 —— 只承认已落库的钱。"""
    async with sessions() as db:
        return await coin_service.treasury_balance(db, slug)


# --------------------------------------------------------------------------- #
# treasury_debit_pending                                                       #
# --------------------------------------------------------------------------- #

async def test_debit_pending_deducts_when_funded(sessions):
    await _seed(sessions, alice=10)

    async with sessions() as db:
        assert await coin_service.treasury_debit_pending(db, "alice", 4) is True
        await db.commit()

    assert await _balance(sessions, "alice") == 6


async def test_debit_pending_short_balance_is_noop(sessions):
    await _seed(sessions, alice=3)

    async with sessions() as db:
        assert await coin_service.treasury_debit_pending(db, "alice", 4) is False
        await db.commit()

    assert await _balance(sessions, "alice") == 3


async def test_debit_pending_missing_row_returns_false(sessions):
    async with sessions() as db:
        assert await coin_service.treasury_debit_pending(db, "ghost", 1) is False
        await db.commit()

    assert await _balance(sessions, "ghost") == 0


@pytest.mark.parametrize("amount", [0, -1, True, 1.5, "1"])
async def test_debit_pending_rejects_bad_amount(sessions, amount):
    async with sessions() as db:
        with pytest.raises(CoinError):
            await coin_service.treasury_debit_pending(db, "alice", amount)


@pytest.mark.parametrize("slug", ["", None, "x" * 101])
async def test_debit_pending_rejects_bad_slug(sessions, slug):
    async with sessions() as db:
        with pytest.raises(CoinError):
            await coin_service.treasury_debit_pending(db, slug, 1)


async def test_debit_pending_is_flush_owned(sessions):
    """不 commit 就丢弃 session → 这笔扣款必须彻底不存在。"""
    await _seed(sessions, alice=10)

    async with sessions() as db:
        assert await coin_service.treasury_debit_pending(db, "alice", 4) is True
        # 另一条连接此刻仍应看到原额 —— 原语自己没有 commit。
        assert await _balance(sessions, "alice") == 10

    assert await _balance(sessions, "alice") == 10


# --------------------------------------------------------------------------- #
# treasury_transfer                                                            #
# --------------------------------------------------------------------------- #

async def test_transfer_moves_money_in_a_single_commit(sessions):
    await _seed(sessions, alice=10, bob=1)

    async with sessions() as db:
        commits = []
        real_commit = db.commit

        async def counting_commit():
            commits.append(1)
            await real_commit()

        db.commit = counting_commit
        assert await coin_service.treasury_transfer(db, "alice", "bob", 4, "npc_trade") is True
        assert commits == [1]

    assert await _balance(sessions, "alice") == 6
    assert await _balance(sessions, "bob") == 5


async def test_transfer_short_payer_leaves_both_sides_untouched(sessions):
    await _seed(sessions, alice=3, bob=1)

    async with sessions() as db:
        assert await coin_service.treasury_transfer(db, "alice", "bob", 4) is False
        await db.commit()

    assert await _balance(sessions, "alice") == 3
    assert await _balance(sessions, "bob") == 1


async def test_transfer_upserts_missing_payee_row(sessions):
    await _seed(sessions, alice=10)

    async with sessions() as db:
        assert await coin_service.treasury_transfer(db, "alice", "bob", 4) is True

    assert await _balance(sessions, "alice") == 6
    assert await _balance(sessions, "bob") == 4


async def test_transfer_to_self_is_rejected(sessions):
    await _seed(sessions, alice=10)

    async with sessions() as db:
        with pytest.raises(CoinError):
            await coin_service.treasury_transfer(db, "alice", "alice", 4)
        await db.commit()

    assert await _balance(sessions, "alice") == 10


async def test_transfer_rolls_back_pending_debit_when_credit_explodes(sessions, monkeypatch):
    """credit 段炸了 → 悬挂的半笔 debit 必须当场回滚,不能搭后续无关 commit 落库。"""
    await _seed(sessions, alice=10, bob=1)

    async def boom(*args, **kwargs):
        raise RuntimeError("credit exploded")

    monkeypatch.setattr(coin_service, "treasury_credit_pending", boom)

    async with sessions() as db:
        with pytest.raises(RuntimeError):
            await coin_service.treasury_transfer(db, "alice", "bob", 4)
        # 同一 session 后续一笔无关写入照常提交 —— 扣款不能被它捎带落库。
        db.add(ResidentTreasury(resident_slug="carol", balance_sc=7))
        await db.commit()

    assert await _balance(sessions, "carol") == 7
    assert await _balance(sessions, "alice") == 10
    assert await _balance(sessions, "bob") == 1
