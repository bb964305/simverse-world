"""C3 NPC 接单：委托承接方从「只能是玩家」扩到「玩家或居民」的载体。

本文件先落 Step 6 的迁移/模型往返（暗上：只加列不改行为），Step 7 再在同一
文件里续接单/结算 pass 的行为测试。

列形态刻意与同表 ``issuer_resident_id`` 对齐：``sa.String`` 无 FK、带索引
（022_add_commissions.py:23-30）——commissions 表整表都不挂 residents 外键，
新列跟随既有风格，避免 purge_residents（手工逐表 delete）路径上多出一条约束。
``acceptor_user_id`` 与 ``acceptor_resident_id`` 是互斥的两列而非合并成一列：
前者对 users.id、后者对 residents.id，玩家已接的单必须能被 NPC pass 逐字节
识别为「别碰」。
"""
from datetime import datetime, timedelta, UTC
from pathlib import Path

import random

import sqlalchemy as sa
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (AsyncSession, async_sessionmaker,
                                    create_async_engine)

from app.config import settings
from app.database import Base
from app.models.commission import Commission
from app.models.memory import Memory
from app.models.resident import Resident
from app.models.resident_treasury import ResidentTreasury
from app.services import coin_service, npc_trade_service


def test_migration_single_head_and_chains_onto_054():
    """`alembic heads` 单头，且新迁移挂在本 worktree 实测的链头 054 上。

    revision id 是文件名 stem（``054_freeze_lab_model_cost_rate``）而不是裸
    ``"054"``，写错会静默断链成双头。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    heads = script.get_heads()
    assert len(heads) == 1, f"alembic multi-head: {heads}"
    rev = script.get_revision("055_add_commission_acceptor")
    assert rev.down_revision == "054_freeze_lab_model_cost_rate"
    # 055 必须在链上(不是必须是链头)——原来这里断言的是 `heads == ["055..."]`,
    # 那会让**任何**后续迁移都把这条弄红(M-A 加固的 056 就是第一个)。真正要守的
    # 是"单头 + 055 挂在 054 上 + 055 仍是 head 的祖先"。
    assert any(r.revision == "055_add_commission_acceptor"
               for r in script.iterate_revisions(heads[0], "base"))


def test_every_revision_id_fits_alembic_version_column():
    """alembic 自建的 ``alembic_version.version_num`` 是 ``varchar(32)``：超长的
    revision id 在 sqlite 上悄悄过、在真 PostgreSQL 上 upgrade 末尾才炸
    （StringDataRightTruncationError，本步实测复现过一次）。整链一起守。
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    script = ScriptDirectory.from_config(Config(str(ini)))
    too_long = [r.revision for r in script.walk_revisions() if len(r.revision) > 32]
    assert too_long == [], f"revision id 超过 alembic_version 的 32 字符上限: {too_long}"


def test_migration_is_additive_only():
    """暗上边界的机器可查版本：只许加列/建索引，不许出现任何数据写语句。"""
    import ast

    path = (Path(__file__).resolve().parent.parent / "alembic" / "versions"
            / "055_add_commission_acceptor.py")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    allowed_ops = {"add_column", "create_index", "drop_index", "drop_column",
                   "batch_alter_table"}
    forbidden_sql = ("insert", "update", "delete")
    called: list[str] = []
    literals: list[str] = []
    for fn in tree.body:
        if not isinstance(fn, ast.FunctionDef) or fn.name not in ("upgrade", "downgrade"):
            continue
        body = fn.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]                      # 跳过函数自己的 docstring
        for stmt in body:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                        if func.value.id in ("op", "batch"):
                            called.append(func.attr)
                elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                    literals.append(node.value)

    assert called, "没扫到任何 op.* 调用 —— 迁移文件形状变了，先看一眼再改守卫"
    assert set(called) <= allowed_ops, (
        f"暗上迁移只许 {sorted(allowed_ops)}，实际出现了 {sorted(set(called))}")
    offenders = [s for s in literals if any(kw in s.lower() for kw in forbidden_sql)]
    assert offenders == [], f"迁移正文里出现了数据写语句字符串 {offenders}"


def test_model_shape_matches_issuer_column():
    cols = Commission.__table__.columns
    col = cols["acceptor_resident_id"]
    assert isinstance(col.type, sa.String)
    assert col.nullable is True
    assert col.index is True
    # 与同表 issuer_resident_id 同形（String 无 FK），跟随 022 的既有风格
    assert col.foreign_keys == set()
    assert type(col.type) is type(cols["issuer_resident_id"].type)


@pytest.mark.anyio
async def test_acceptor_resident_id_defaults_none_and_roundtrips(db_engine):
    """新 session 重读断言：默认 None，可写可读。"""
    factory = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        r = Resident(slug="tie-sheng", name="陈铁生", creator_id="system",
                     district="central_plaza", status="idle", tile_x=1, tile_y=1)
        db.add(r)
        await db.flush()
        c = Commission(issuer_resident_id=r.id, kind="chat_topic", title="带个话",
                       payload_json={"target_slug": "tie-sheng"}, reward_sc=8)
        db.add(c)
        await db.commit()
        cid, rid = c.id, r.id

    async with factory() as db:
        row = (await db.execute(select(Commission).where(Commission.id == cid))).scalar_one()
        assert row.acceptor_resident_id is None
        assert row.acceptor_user_id is None
        row.acceptor_resident_id = rid
        await db.commit()

    async with factory() as db:
        row = (await db.execute(select(Commission).where(Commission.id == cid))).scalar_one()
        assert row.acceptor_resident_id == rid
        assert row.acceptor_user_id is None


# =========================================================================== #
# Step 7 — 接单/结算 pass(guarded 占坑单事务付款)                              #
#                                                                             #
# 断言**一律新开 session 重读**(理由同 test_npc_consumption):conftest 的       #
# `:memory:` 引擎走 StaticPool,所有 session 共用一条连接、读得到尚未 commit 的  #
# 改动,"单事务"的边界会假绿,所以本节自建文件型 sqlite。fixture 故意让          #
# `Resident.id` ≠ `slug` —— 委托两列存的是 id、钱包按 slug 记账,串了就露馅。   #
# =========================================================================== #


@pytest.fixture
async def sessions(tmp_path):
    """Session factory on a file-backed sqlite — real per-session connections."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'commission.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
def trade_on(monkeypatch):
    """C3 闸开。掷骰概率拍成 1.0 —— 本节测的是事务边界与守卫,不是骰子。"""
    monkeypatch.setattr(settings, "npc_economy_enabled", True)
    monkeypatch.setattr(settings, "npc_trade_enabled", True)
    monkeypatch.setattr(settings, "npc_trade_buy_prob", 1.0)
    monkeypatch.setattr(settings, "npc_commission_accept_prob", 1.0)
    return settings


@pytest.fixture
def feed_pushes(monkeypatch):
    """记录 feed 推送——push 自开 session 打全局引擎,测试里必须换掉。"""
    from app.services import feed_service

    calls: list[tuple] = []

    async def _fake(resident_slug, kind, payload=None):
        calls.append((resident_slug, kind, payload or {}))

    monkeypatch.setattr(feed_service, "push", _fake)
    return calls


def _res(rid, slug, name, resident_type="npc"):
    """id 与 slug 故意不同形(委托记 id、钱包记 slug)。"""
    return Resident(id=rid, slug=slug, name=name, district="free", status="idle",
                    resident_type=resident_type)


def _commission(issuer_id, *, title="带个话", reward=8, status="open",
                acceptor_resident_id=None, acceptor_user_id=None, hours=48):
    return Commission(issuer_resident_id=issuer_id, kind="chat_topic", title=title,
                      payload_json={}, reward_sc=reward, status=status,
                      acceptor_resident_id=acceptor_resident_id,
                      acceptor_user_id=acceptor_user_id,
                      expires_at=datetime.now(UTC) + timedelta(hours=hours))


async def _seed(sessions, residents=(), commissions=(), **balances: int) -> list[str]:
    async with sessions() as db:
        db.add_all(list(residents) + list(commissions))
        for slug, amount in balances.items():
            db.add(ResidentTreasury(resident_slug=slug, balance_sc=amount))
        await db.commit()
        return [c.id for c in commissions]


async def _settle(sessions) -> dict:
    async with sessions() as db:
        return await npc_trade_service.run_commission_settle_pass(db)


async def _accept(sessions, seed: int = 42) -> dict:
    async with sessions() as db:
        return await npc_trade_service.run_commission_accept_pass(
            db, rng=random.Random(seed))


async def _row(sessions, cid: str) -> Commission:
    async with sessions() as db:
        return (await db.execute(
            select(Commission).where(Commission.id == cid))).scalar_one()


async def _balance(sessions, slug: str) -> int:
    """新 session 重读 —— 只承认已落库的钱。"""
    async with sessions() as db:
        return await coin_service.treasury_balance(db, slug)


async def _wallet(sessions, slug: str):
    async with sessions() as db:
        r = (await db.execute(select(Resident).where(Resident.slug == slug))).scalar_one()
        return (r.meta_json or {}).get("wallet")


async def _memories(sessions, resident_id: str) -> list[str]:
    async with sessions() as db:
        rows = (await db.execute(
            select(Memory.content).where(Memory.resident_id == resident_id)
        )).scalars().all()
    return list(rows)


# --------------------------------------------------------------------------- #
# 1. 闸关(任一)→ 两 pass 都 no-op 零写入                                       #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
@pytest.mark.parametrize("off_key", ["npc_trade_enabled", "npc_economy_enabled"])
async def test_gate_off_makes_both_passes_noop(sessions, trade_on, feed_pushes,
                                               monkeypatch, off_key):
    monkeypatch.setattr(settings, off_key, False)
    [open_id, taken_id] = await _seed(
        sessions,
        [_res("id-issuer-001", "tie-sheng", "陈铁生"),
         _res("id-worker-001", "a-lan", "阿岚")],
        [_commission("id-issuer-001"),
         _commission("id-issuer-001", status="accepted",
                     acceptor_resident_id="id-worker-001")],
        **{"tie-sheng": 20},
    )

    assert await _settle(sessions) == {"settled": 0, "paid": 0, "reopened": 0}
    assert await _accept(sessions) == {"accepted": 0}

    assert (await _row(sessions, open_id)).status == "open"
    taken = await _row(sessions, taken_id)
    assert (taken.status, taken.completed_at) == ("accepted", None)
    assert await _balance(sessions, "tie-sheng") == 20
    assert await _balance(sessions, "a-lan") == 0
    assert await _memories(sessions, "id-worker-001") == []
    assert feed_pushes == []


# --------------------------------------------------------------------------- #
# 2. settle:单事务把赏金从发单人搬到承接人                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_settle_moves_the_reward_and_narrates(sessions, trade_on, feed_pushes):
    [cid] = await _seed(
        sessions,
        [_res("id-issuer-001", "tie-sheng", "陈铁生"),
         _res("id-worker-001", "a-lan", "阿岚")],
        [_commission("id-issuer-001", title="带个话", status="accepted",
                     acceptor_resident_id="id-worker-001")],
        **{"tie-sheng": 20, "a-lan": 3},
    )

    assert await _settle(sessions) == {"settled": 1, "paid": 8, "reopened": 0}

    row = await _row(sessions, cid)
    assert row.status == "completed"
    assert row.completed_at is not None
    assert row.acceptor_user_id is None            # 玩家路径的列一律不碰
    assert row.acceptor_resident_id == "id-worker-001"

    assert await _balance(sessions, "tie-sheng") == 12   # 发单人真实出资,不是铸币
    assert await _balance(sessions, "a-lan") == 11
    assert await _wallet(sessions, "tie-sheng") == 12    # 双方钱包缓存都刷
    assert await _wallet(sessions, "a-lan") == 11

    assert any("带个话" in m for m in await _memories(sessions, "id-issuer-001"))
    assert any("带个话" in m for m in await _memories(sessions, "id-worker-001"))
    assert sorted((slug, kind) for slug, kind, _ in feed_pushes) == [
        ("a-lan", "npc_commission_done"), ("tie-sheng", "npc_commission_done")]


@pytest.mark.anyio
async def test_settle_never_touches_a_player_accepted_commission(sessions, trade_on,
                                                                 feed_pushes):
    """玩家已接的单(acceptor_user_id 非空)是另一条铸币路径,NPC pass 一律不碰。"""
    [cid] = await _seed(
        sessions,
        [_res("id-issuer-001", "tie-sheng", "陈铁生")],
        [_commission("id-issuer-001", status="accepted", acceptor_user_id="user-1")],
        **{"tie-sheng": 20},
    )

    assert await _settle(sessions) == {"settled": 0, "paid": 0, "reopened": 0}
    row = await _row(sessions, cid)
    assert (row.status, row.acceptor_user_id) == ("accepted", "user-1")
    assert await _balance(sessions, "tie-sheng") == 20
    assert feed_pushes == []


# --------------------------------------------------------------------------- #
# 3. settle 的崩溃语义:commit 前炸掉 → 钱与状态都没动(重跑不重复付款)          #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_settle_crash_before_commit_leaves_money_and_status_untouched(
        sessions, trade_on, feed_pushes, monkeypatch):
    [cid] = await _seed(
        sessions,
        [_res("id-issuer-001", "tie-sheng", "陈铁生"),
         _res("id-worker-001", "a-lan", "阿岚")],
        [_commission("id-issuer-001", status="accepted",
                     acceptor_resident_id="id-worker-001")],
        **{"tie-sheng": 20},
    )

    async def _boom(db, slug, amount, reason=""):
        raise RuntimeError("credit leg exploded")

    monkeypatch.setattr(coin_service, "treasury_credit_pending", _boom)

    assert await _settle(sessions) == {"settled": 0, "paid": 0, "reopened": 0}

    row = await _row(sessions, cid)
    assert (row.status, row.completed_at) == ("accepted", None)   # 占坑被回滚
    assert row.acceptor_resident_id == "id-worker-001"
    assert await _balance(sessions, "tie-sheng") == 20            # 悬挂 debit 没落库
    assert await _balance(sessions, "a-lan") == 0
    assert feed_pushes == []


# --------------------------------------------------------------------------- #
# 4. 发单人付不起 / 一方已被清号 → 回 open 清 acceptor,不转账                   #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_broke_issuer_reopens_the_commission(sessions, trade_on, feed_pushes):
    [cid] = await _seed(
        sessions,
        [_res("id-issuer-001", "tie-sheng", "陈铁生"),
         _res("id-worker-001", "a-lan", "阿岚")],
        [_commission("id-issuer-001", status="accepted",
                     acceptor_resident_id="id-worker-001")],
        **{"tie-sheng": 3},
    )

    assert await _settle(sessions) == {"settled": 0, "paid": 0, "reopened": 1}

    row = await _row(sessions, cid)
    assert row.status == "open"                    # 由既有 48h 过期扫尾
    assert row.acceptor_resident_id is None
    assert row.completed_at is None
    assert await _balance(sessions, "tie-sheng") == 3
    assert await _balance(sessions, "a-lan") == 0


@pytest.mark.anyio
@pytest.mark.parametrize("missing", ["issuer", "acceptor"])
async def test_settle_reopens_when_a_party_is_gone(sessions, trade_on, feed_pushes,
                                                   missing):
    """委托两列存的是 Resident.id,居民被 purge 后 id 就是个死引用 —— 流单不转账。"""
    residents = [_res("id-issuer-001", "tie-sheng", "陈铁生"),
                 _res("id-worker-001", "a-lan", "阿岚")]
    residents.pop(0 if missing == "issuer" else 1)
    [cid] = await _seed(
        sessions, residents,
        [_commission("id-issuer-001", status="accepted",
                     acceptor_resident_id="id-worker-001")],
        **{"tie-sheng": 20},
    )

    assert await _settle(sessions) == {"settled": 0, "paid": 0, "reopened": 1}

    row = await _row(sessions, cid)
    assert (row.status, row.acceptor_resident_id) == ("open", None)
    assert await _balance(sessions, "tie-sheng") == 20
    assert await _balance(sessions, "a-lan") == 0
    assert feed_pushes == []


# --------------------------------------------------------------------------- #
# 5. accept:恰一人接单,钱这时还不动                                            #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_accept_picks_exactly_one_autonomous_resident(sessions, trade_on,
                                                            feed_pushes):
    [cid] = await _seed(
        sessions,
        [_res("id-issuer-001", "tie-sheng", "陈铁生"),
         _res("id-worker-001", "a-lan", "阿岚"),
         _res("id-worker-002", "wan-qiu", "林晚秋")],
        [_commission("id-issuer-001", title="带个话")],
        **{"tie-sheng": 20},
    )

    assert await _accept(sessions) == {"accepted": 1}

    row = await _row(sessions, cid)
    assert row.status == "accepted"
    assert row.acceptor_resident_id in ("id-worker-001", "id-worker-002")
    assert row.acceptor_user_id is None
    assert await _balance(sessions, "tie-sheng") == 20      # 接单不付钱

    taker = row.acceptor_resident_id
    taker_slug = {"id-worker-001": "a-lan", "id-worker-002": "wan-qiu"}[taker]
    assert any("带个话" in m for m in await _memories(sessions, taker))
    assert any("带个话" in m for m in await _memories(sessions, "id-issuer-001"))
    assert sorted(slug for slug, _, _ in feed_pushes) == sorted(["tie-sheng", taker_slug])
    assert {kind for _, kind, _ in feed_pushes} == {"npc_commission_taken"}


@pytest.mark.anyio
async def test_accept_probability_is_independent_from_product_buying(
    sessions, trade_on, feed_pushes, monkeypatch,
):
    monkeypatch.setattr(settings, "npc_trade_buy_prob", 0.0)
    monkeypatch.setattr(settings, "npc_commission_accept_prob", 1.0)
    [cid] = await _seed(
        sessions,
        [_res("id-issuer-prob", "issuer-prob", "发单人"),
         _res("id-worker-prob", "worker-prob", "承接人")],
        [_commission("id-issuer-prob")],
        **{"issuer-prob": 20},
    )

    assert await _accept(sessions) == {"accepted": 1}
    assert (await _row(sessions, cid)).status == "accepted"


@pytest.mark.anyio
async def test_accept_skips_a_broke_issuer(sessions, trade_on, feed_pushes):
    """接单前先看发单人付不付得起 —— 别让人白跑一趟。"""
    [cid] = await _seed(
        sessions,
        [_res("id-issuer-001", "tie-sheng", "陈铁生"),
         _res("id-worker-001", "a-lan", "阿岚")],
        [_commission("id-issuer-001")],
        **{"tie-sheng": 3},
    )

    assert await _accept(sessions) == {"accepted": 0}
    row = await _row(sessions, cid)
    assert (row.status, row.acceptor_resident_id) == ("open", None)
    assert feed_pushes == []


@pytest.mark.anyio
async def test_accept_skips_the_issuer_and_non_autonomous_residents(sessions, trade_on,
                                                                    feed_pushes):
    """候选池只有"发单人自己 + 玩家化身" → 无人可接,单子留在 open。"""
    [cid] = await _seed(
        sessions,
        [_res("id-issuer-001", "tie-sheng", "陈铁生"),
         _res("id-player-001", "avatar", "玩家化身", resident_type="player")],
        [_commission("id-issuer-001")],
        **{"tie-sheng": 20},
    )

    assert await _accept(sessions) == {"accepted": 0}
    row = await _row(sessions, cid)
    assert (row.status, row.acceptor_resident_id) == ("open", None)
    assert feed_pushes == []


@pytest.mark.anyio
async def test_accept_skips_expired_commissions(sessions, trade_on, feed_pushes):
    [cid] = await _seed(
        sessions,
        [_res("id-issuer-001", "tie-sheng", "陈铁生"),
         _res("id-worker-001", "a-lan", "阿岚")],
        [_commission("id-issuer-001", hours=-1)],
        **{"tie-sheng": 20},
    )

    assert await _accept(sessions) == {"accepted": 0}
    assert (await _row(sessions, cid)).status == "open"     # 由 nightly #3 扫成 expired
    assert feed_pushes == []


@pytest.mark.anyio
async def test_one_commission_per_acceptor_per_night(sessions, trade_on, feed_pushes):
    """同一个人一晚只接一单 —— 与 C2 的"每人一笔"同口径。"""
    ids = await _seed(
        sessions,
        [_res("id-issuer-001", "tie-sheng", "陈铁生"),
         _res("id-worker-001", "a-lan", "阿岚")],
        [_commission("id-issuer-001", title="带个话"),
         _commission("id-issuer-001", title="捎件东西")],
        **{"tie-sheng": 40},
    )

    assert await _accept(sessions) == {"accepted": 1}
    statuses = sorted([(await _row(sessions, cid)).status for cid in ids])
    assert statuses == ["accepted", "open"]


# --------------------------------------------------------------------------- #
# 6. accept 的并发守卫:扫描后被别人抢走 → guarded UPDATE rowcount=0 放弃         #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_accept_gives_up_when_the_guard_loses_the_race(sessions, trade_on,
                                                             feed_pushes, monkeypatch):
    [cid] = await _seed(
        sessions,
        [_res("id-issuer-001", "tie-sheng", "陈铁生"),
         _res("id-worker-001", "a-lan", "阿岚")],
        [_commission("id-issuer-001")],
        **{"tie-sheng": 20},
    )

    real = npc_trade_service._acceptor_candidates

    async def _steal_then_list(db, issuer_id):
        # 扫描与占坑之间玩家抢先接走(commission_service.accept 的同款 guarded
        # UPDATE),NPC 这边必须认输,不能把玩家的单改写成居民的。
        await db.execute(
            sa.update(Commission).where(Commission.id == cid)
            .values(status="accepted", acceptor_user_id="user-1")
            .execution_options(synchronize_session=False))
        await db.commit()
        return await real(db, issuer_id)

    monkeypatch.setattr(npc_trade_service, "_acceptor_candidates", _steal_then_list)

    assert await _accept(sessions) == {"accepted": 0}
    row = await _row(sessions, cid)
    assert (row.status, row.acceptor_user_id) == ("accepted", "user-1")
    assert row.acceptor_resident_id is None
    assert feed_pushes == []


# --------------------------------------------------------------------------- #
# 7. 同晚 accept 不被同晚 settle(先结算后接单的顺序就是"不需要新时间戳列"的理由) #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_tonights_accept_is_not_settled_tonight(sessions, trade_on, feed_pushes):
    [cid] = await _seed(
        sessions,
        [_res("id-issuer-001", "tie-sheng", "陈铁生"),
         _res("id-worker-001", "a-lan", "阿岚")],
        [_commission("id-issuer-001")],
        **{"tie-sheng": 20},
    )

    assert await _settle(sessions) == {"settled": 0, "paid": 0, "reopened": 0}
    assert await _accept(sessions) == {"accepted": 1}

    row = await _row(sessions, cid)
    assert (row.status, row.completed_at) == ("accepted", None)
    assert await _balance(sessions, "tie-sheng") == 20
    assert await _balance(sessions, "a-lan") == 0
