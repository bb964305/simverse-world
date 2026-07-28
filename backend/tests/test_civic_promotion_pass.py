"""F2 Task 12 —— 晋升 pass 的三态与四道数值闸门。

shadow 的准确定位：首夜爆炸半径不可预演，它是**带全部防呆的实跑演练 + 名单
落盘**，不是「规模在开闸前无人知晓」（只读标定本来就能测出候选规模）。

结构性收口（Task 6 评审硬要求，逐字引用）：「``select_promotions`` is only a
DECISION function; the actual DB write happens when Task 12 calls
``civic_membership.grant_citizenship_batch``. The gate therefore blocks
"calling ``select_promotions(mode=on)``", NOT "any write". Nothing
structurally forces Task 12 through the gate at all. FIX SHAPE: give Task 12
a dedicated entry point with ``mode='on'`` hardcoded (e.g.
``select_promotions_for_write(...)``) rather than letting it reuse the
shadow-defaulting signature.」—— 本文件「写路径的结构性收口」一节用 spy /
桩两种手法直接证明这个洞已经补上，而不是只靠 DB 状态断言侧面印证。
"""
import json
import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.models.civic_standing_history import CivicStandingHistory
from app.models.resident import Resident
from app.models.resident_relation import ResidentRelation
from app.models.user import User
from app.services import civic_membership as cm
from app.services.config_service import ConfigService
from app.tasks import civic_promotion as cp

BACKEND_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _res(slug, rtype, *, creator_id="u1", meta=None, days=200):
    return Resident(slug=slug, name=slug, district="town_hall", status="idle",
                    resident_type=rtype, creator_id=creator_id, tile_x=1,
                    tile_y=1, meta_json=meta,
                    created_at=datetime.now(UTC) - timedelta(days=days))


async def _world(db, *, builtins=4, denizens=1, edges_per=2):
    """一个「全员达标」的小世界：denizens 与前 edges_per 位内置公民都够熟。"""
    bs = [_res(f"b{i}", cm.CIVIC_MEMBER_TYPE, creator_id=cm.SYSTEM_CREATOR_ID,
               meta={"origin": "preset"}) for i in range(builtins)]
    us = [_res(f"u{i}", cm.UGC_RESIDENT_TYPE, meta={"origin": "forge"})
          for i in range(denizens)]
    db.add_all(bs + us)
    await db.commit()
    for u in us:
        for b in bs[:edges_per]:
            a_id, b_id = sorted([u.id, b.id])
            db.add(ResidentRelation(party_a=a_id, party_b=b_id,
                                    familiarity=0.6))
    await db.commit()
    return bs, us


@pytest.fixture(autouse=True)
def _thresholds(monkeypatch):
    """全部旋钮显式置成默认值，用例不依赖 env 的外部状态。

    ⚠️ `CIVIC_PROMOTION_BREAKER_MIN_ABS` 必须显式设：小世界夹具（4 位内置公民）
    的比例项只有 4 × 0.20 = 0.8，没有绝对下限的话**任何一个候选都会触发熔断**，
    on 态与 shadow 态的用例全部废掉（shadow 分支在熔断 return 之后，会变成空跑）。
    """
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_WORLD_DAYS", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "2")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_FAMILIARITY", "0.2")
    monkeypatch.setenv("CIVIC_PEER_SEASONING_WORLD_DAYS", "28")
    monkeypatch.setenv("CIVIC_PROMOTION_MAX_PER_RUN", "5")
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_FRACTION", "0.20")
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_MIN_ABS", "3")
    monkeypatch.delenv("CIVIC_AUTO_DEMOTION_ENABLED", raising=False)
    yield


# ── off ────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_default_mode_with_no_env_set_is_off_and_a_noop(db_session,
                                                               monkeypatch):
    """默认态（完全不碰 ``CIVIC_PROMOTION_MODE``）必须是 off——合并本分支
    不得改变生产行为一个字节。"""
    monkeypatch.delenv("CIVIC_PROMOTION_MODE", raising=False)
    await _world(db_session)

    result = await cp.run_promotion_pass(db_session)
    assert result["mode"] == cp.MODE_OFF
    assert result["promoted"] == 0
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0
    assert await ConfigService(db_session).get(cp.RUN_SUMMARY_KEY) is None


@pytest.mark.anyio
async def test_off_is_a_zero_read_zero_write_noop(db_session, monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "off")
    await _world(db_session)

    result = await cp.run_promotion_pass(db_session)
    assert result["mode"] == cp.MODE_OFF
    assert result["promoted"] == 0
    assert result["candidates"] == []
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0
    assert await ConfigService(db_session).get(cp.RUN_SUMMARY_KEY) is None


@pytest.mark.anyio
async def test_unknown_mode_degrades_to_off(db_session, monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "ON!!")
    await _world(db_session)
    assert (await cp.run_promotion_pass(db_session))["mode"] == cp.MODE_OFF


# ── shadow ─────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_shadow_computes_the_list_but_writes_no_politics(
        db_session, monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "shadow")
    _, us = await _world(db_session, denizens=2)

    result = await cp.run_promotion_pass(db_session)
    assert result["mode"] == cp.MODE_SHADOW
    # 必须真的走到 shadow 分支：熔断的 return 在 `if mode == MODE_SHADOW` 之前，
    # 一旦熔断先响，本用例会以「promoted == 0」侥幸全绿而 shadow 分支一行没跑
    assert result["refused"] is None
    assert result["promoted"] == 0
    assert sorted(result["candidates"]) == ["u0", "u1"]
    assert result["evidence"]["u0"]["peers"] == 2
    assert result["evidence"]["u0"]["world_days"] > 0

    # 政治层零写入
    types = (await db_session.execute(
        select(Resident.resident_type).where(
            Resident.slug.in_(["u0", "u1"])))).scalars().all()
    assert set(types) == {cm.UGC_RESIDENT_TYPE}
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0


@pytest.mark.anyio
async def test_shadow_records_the_run_summary_for_the_probe(db_session,
                                                            monkeypatch):
    """shadow 不产生历史行，探针没有别的载体——运行摘要是 shadow 的唯一写。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "shadow")
    await _world(db_session, denizens=1)
    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] is None, "熔断先响的话本用例是空跑（摘要两条路都写）"

    summary = await ConfigService(db_session).get(cp.RUN_SUMMARY_KEY)
    assert summary["mode"] == cp.MODE_SHADOW
    assert summary["candidates"] == ["u0"]
    assert summary["promoted"] == 0
    assert summary["refused"] is None
    assert "world_at" in summary


@pytest.mark.anyio
async def test_shadow_summary_write_does_not_commit_unrelated_pending_state(
        monkeypatch, tmp_path):
    """复审 Important 2：shadow 唯一的写（运行摘要）不能借道调用方 session
    自己的事务边界。``_record_run`` → ``ConfigService.set`` 末尾是
    ``await self._db.commit()``；``run_promotion_pass`` 对调用方传入什么样
    的 session 没有任何契约保证——如果 ``db`` 上还有别的未提交改动（比如
    接进 nightly_cron 后与其它任务共用同一个 session，或是被某个手工脚本
    复用的长命 session），直接在 ``db`` 上 commit 会把那些改动一并带下去。

    ⚠️ 这里**不用** ``db_session`` 夹具——那个夹具背后的引擎是 SQLite
    ``:memory:`` + ``StaticPool``：全程只有一个物理连接，任何"专用 session"
    最终都会拿到同一个连接、同一个事务，天然验证不出连接级隔离（实测过：
    在 ``:memory:``/``StaticPool`` 上，即使 ``_record_run`` 已经改成开专用
    session，``scratch.commit()`` 命中的还是 ``db_session`` 那个唯一的物理
    连接，intruder 一样会被带下去——不是修复没生效，是这个夹具结构性验证不
    出"两个 session 各自独立提交"这件事）。生产是 Postgres 的真实连接池
    （``pool_size=20``），这里改用临时文件 SQLite（``AsyncAdaptedQueuePool``，
    真正的多连接、彼此独立的事务）来如实模拟"专用 session 拿到独立物理连接"。

    用 rollback 之后还在不在来判定"是不是已经被提交"：同一个 session 内，
    真正提交过的行 rollback 不掉。
    """
    from sqlalchemy.ext.asyncio import (
        AsyncSession as _AsyncSession, async_sessionmaker as _async_sessionmaker,
        create_async_engine as _create_async_engine,
    )

    from app.database import Base as _Base

    engine = _create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'record_run_isolation.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    # autoflush=False：SQLite 文件库是单写者锁——build_snapshot() 的只读
    # SELECT 若 autoflush 出 intruder 的 INSERT，会在这个连接上一直握着写锁
    # 直到 db_session 提交/回滚，而 _record_run 的专用 session 这时去写
    # system_config 会撞上同一个文件锁，报 "database is locked"（这是 SQLite
    # 单写者模型的产物，Postgres 的行级 MVCC 不会有这个问题——同一张不相关
    # 的表，两个事务互不阻塞）。关掉 autoflush 只是不让"待提交但从未真正发
    # 送给数据库"的对象提前触发写锁，不影响本测试要证明的东西：intruder
    # 在 db_session 自己 commit 之前，任何时候都不该真的落库。
    factory = _async_sessionmaker(engine, class_=_AsyncSession,
                                  expire_on_commit=False, autoflush=False)

    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "shadow")
    async with factory() as db_session:
        await _world(db_session, denizens=1)

        intruder = _res("intruder_pending_row", cm.UGC_RESIDENT_TYPE)
        db_session.add(intruder)   # 故意不 commit——模拟共享 session 里别的
                                   # 任务留下的未提交改动

        result = await cp.run_promotion_pass(db_session)
        assert result["mode"] == cp.MODE_SHADOW

        await db_session.rollback()
        intruder_survived = (await db_session.execute(
            select(Resident.slug).where(Resident.slug == "intruder_pending_row")
        )).scalar_one_or_none()
        assert intruder_survived is None, (
            "shadow 的运行摘要写把 session 里不相关的待提交改动一并 commit 了")

        # shadow 自己那一次写（运行摘要）必须真的落库——不能为了不牵连
        # intruder 就干脆什么都不写了
        summary = await ConfigService(db_session).get(cp.RUN_SUMMARY_KEY)
        assert summary is not None
        assert summary["mode"] == cp.MODE_SHADOW
        assert summary["candidates"] == ["u0"]
    await engine.dispose()


# ── on ─────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_on_promotes_and_leaves_a_history_row_each(db_session, monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    _, us = await _world(db_session, denizens=2)

    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] is None
    assert result["promoted"] == 2
    voters = (await db_session.execute(
        select(Resident.slug).where(Resident.is_civic_voter))).scalars().all()
    assert {"u0", "u1"} <= set(voters)
    rows = (await db_session.execute(
        select(CivicStandingHistory))).scalars().all()
    assert len(rows) == 2
    assert {r.actor for r in rows} == {cp.PROMOTION_ACTOR}
    assert {r.reason_code for r in rows} == {cp.PROMOTION_REASON_CODE}
    assert all(r.evidence_json.get("peers") == 2 for r in rows)


@pytest.mark.anyio
async def test_running_twice_is_idempotent(db_session, monkeypatch):
    """已晋升的人不再进候选面（select_promotions 只收 denizen 档）。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    await _world(db_session, denizens=1)
    assert (await cp.run_promotion_pass(db_session))["promoted"] == 1
    assert (await cp.run_promotion_pass(db_session))["promoted"] == 0


@pytest.mark.anyio
async def test_pass_never_demotes(db_session, monkeypatch):
    """夜间任务只升，永不自动降——门槛②读的 familiarity 有周衰减，接成降级
    判据等于让公民权跟着社交波动飘。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    bs, _ = await _world(db_session, builtins=4, denizens=0)
    naturalised = _res("naturalised", cm.CIVIC_MEMBER_TYPE,
                       meta={"origin": "forge"})
    db_session.add(naturalised)
    await db_session.commit()

    result = await cp.run_promotion_pass(db_session)
    assert result.get("demoted", 0) == 0
    rtype = (await db_session.execute(
        select(Resident.resident_type)
        .where(Resident.slug == "naturalised"))).scalar_one()
    assert rtype == cm.CIVIC_MEMBER_TYPE


@pytest.mark.anyio
async def test_auto_demotion_flag_raises_instead_of_running_unhedged(
        db_session, monkeypatch):
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    monkeypatch.setenv("CIVIC_AUTO_DEMOTION_ENABLED", "true")
    await _world(db_session)
    with pytest.raises(NotImplementedError, match="滞后"):
        await cp.run_promotion_pass(db_session)


# ── 候选面纪律与拒绝容错（复审 Important 1）───────────────────────────────
#
# 复现的真实路径：admin 手滑把玩家化身的 resident_type 改成 denizen 档，且
# meta_json 没有 origin 键——is_ugc_resident 第 5 条兜底（creator_id is not
# None）会把它判成 UGC，候选面因此收它；写入口（grant_citizenship_batch）
# 查 users.player_resident_id 拒绝整批是防线在生效，但候选面把它端上来这件
# 事本身就是 bug——CivicStandingRefused 是整批拒绝，会让同一批里合法候选跟
# 着永久卡死，而且在当前实现下这个异常不会被捕获，一路抛出 run_promotion_
# pass，接进 nightly_cron 后会中断整条夜间链，run summary 也因为 _record_run
# 从未被调用到而不写——运维在探针上看不到任何线索。

@pytest.mark.anyio
async def test_batch_with_a_player_avatar_promotes_the_rest_and_skips_the_avatar(
        db_session, monkeypatch):
    """候选面必须在自己的防线上把玩家化身筛掉，不能指望写入口的射程检查
    兜底——写入口是整批拒绝，会连累同一批里的合法候选一起卡死。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    bs, us = await _world(db_session, builtins=4, denizens=2)

    avatar = _res("avatar_gone_denizen", cm.UGC_RESIDENT_TYPE, meta=None)
    db_session.add(avatar)
    await db_session.flush()
    for b in bs[:2]:
        a_id, b_id = sorted([avatar.id, b.id])
        db_session.add(ResidentRelation(party_a=a_id, party_b=b_id,
                                        familiarity=0.6))
    db_session.add(User(name="玩家", email="avatar-owner@t.example",
                        player_resident_id=avatar.id))
    await db_session.commit()

    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] is None
    assert result["promoted"] == 2

    voters = (await db_session.execute(
        select(Resident.slug).where(Resident.is_civic_voter))).scalars().all()
    assert {"u0", "u1"} <= set(voters)
    assert "avatar_gone_denizen" not in voters

    avatar_type = (await db_session.execute(
        select(Resident.resident_type)
        .where(Resident.slug == "avatar_gone_denizen"))).scalar_one()
    assert avatar_type == cm.UGC_RESIDENT_TYPE   # 化身档位原封不动

    rows = (await db_session.execute(
        select(CivicStandingHistory))).scalars().all()
    assert len(rows) == 2
    assert avatar.id not in {r.resident_id for r in rows}


@pytest.mark.anyio
async def test_grant_refusal_writes_a_summary_naming_the_reason_instead_of_vanishing(
        db_session, monkeypatch):
    """闸门之外的任何拒绝类都不能让 pass 静默消失——不得把异常抛给调用方
    （nightly_cron 里一炸会中断整条夜间链），run summary 必须带上拒绝原因，
    不是「_record_run 在异常路径之后，从未被调用到」那种半吊子。用 spy 顶替
    grant_citizenship_batch 本身直接模拟拒绝，证明这条容错对**任何**
    CivicStandingRefused 都成立，不只是玩家化身这一类。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    await _world(db_session, denizens=2)

    async def _boom(*args, **kwargs):
        raise cm.CivicStandingRefused("grant refused: simulated race")

    monkeypatch.setattr(cm, "grant_citizenship_batch", _boom)

    result = await cp.run_promotion_pass(db_session)
    assert result["promoted"] == 0
    assert result["refused"] == "grant_refused"
    assert "simulated race" in (result.get("refused_detail") or "")

    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0

    summary = await ConfigService(db_session).get(cp.RUN_SUMMARY_KEY)
    assert summary is not None, "拒绝路径必须也写运行摘要，不能静默消失"
    assert summary["refused"] == "grant_refused"
    assert len(summary.get("refused_detail") or "") <= 300


# ── 写路径的结构性收口（Task 6 评审硬要求）───────────────────────────────
#
# select_promotions 默认 mode=MODE_SHADOW（观测态，任何门槛值都放行）。如果
# 写路径直接复用这个签名（"忘记传 mode='on'"），占位门槛就会悄悄流进
# grant_citizenship_batch——闸门形同虚设。下面四个测试逐一证明：写路径的
# 唯一入口 select_promotions_for_write 把 mode="on" 硬编码、不对外暴露 mode
# 形参，且是 grant_citizenship_batch 唯一可能被触达的路。

def test_select_promotions_for_write_hardcodes_mode_on(monkeypatch):
    """专用写路径入口不暴露 mode 形参——用占位门槛调用必炸，调用方没有
    "忘记传 mode='on'" 的余地。对照：同样的占位门槛喂给 select_promotions()
    的默认（shadow-defaulting）签名不炸。"""
    monkeypatch.delenv("CIVIC_THRESHOLDS_CALIBRATED", raising=False)
    snap = cp.PromotionSnapshot(now_world=datetime(2026, 8, 1, tzinfo=UTC),
                                facts=(), familiarity=())
    placeholder_kw = dict(min_world_days=cp.PLACEHOLDER_THRESHOLDS[0],
                          min_peers=cp.PLACEHOLDER_THRESHOLDS[1],
                          min_familiarity=cp.PLACEHOLDER_THRESHOLDS[2],
                          seasoning_days=28.0)

    with pytest.raises(cp.UncalibratedThresholds):
        cp.select_promotions_for_write(snap, **placeholder_kw)

    assert cp.select_promotions(snap, **placeholder_kw) == ()


@pytest.mark.anyio
async def test_on_mode_raises_uncalibrated_thresholds_end_to_end(db_session,
                                                                  monkeypatch):
    """占位门槛端到端拒绝：mode=on 时，就算世界里已经有合法候选，整条 pass
    必须在**任何写入之前**炸出来——run summary 也不写（``_record_run`` 从
    未被调用到），不是「拒绝了但摘要两条路都写」那种半吊子。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    monkeypatch.delenv("CIVIC_THRESHOLDS_CALIBRATED", raising=False)
    # 覆盖 autouse 夹具，改回与 PLACEHOLDER_THRESHOLDS 逐字相同
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_WORLD_DAYS", "30")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_PEERS", "3")
    monkeypatch.setenv("CIVIC_PROMOTION_MIN_FAMILIARITY", "0.20")
    await _world(db_session, builtins=4, denizens=1, edges_per=3)

    with pytest.raises(cp.UncalibratedThresholds):
        await cp.run_promotion_pass(db_session)

    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0
    assert await ConfigService(db_session).get(cp.RUN_SUMMARY_KEY) is None


@pytest.mark.anyio
async def test_write_entrypoint_unreachable_when_gate_off_or_shadow(
        db_session, monkeypatch):
    """结构性证明：能触达 grant_citizenship_batch 的唯一候选来源是
    select_promotions_for_write。用 spy 顶替它：off/shadow 两态下调用次数
    必须是 0——不是「调用了但没生效」，是从未进入那段代码路径；on 态下必须
    恰好调用一次。同一个 spy、同一个世界，跨三态观察，证明 shadow 与 on 在
    「有没有走到写路径入口」这件事上是结构性不同，不是行为上凑巧不同。"""
    calls = []
    real_entry = cp.select_promotions_for_write

    def spy(*args, **kwargs):
        calls.append((args, kwargs))
        return real_entry(*args, **kwargs)

    monkeypatch.setattr(cp, "select_promotions_for_write", spy)
    await _world(db_session, denizens=2)

    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "off")
    off_result = await cp.run_promotion_pass(db_session)
    assert off_result["mode"] == cp.MODE_OFF
    assert calls == []

    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "shadow")
    shadow_result = await cp.run_promotion_pass(db_session)
    assert shadow_result["mode"] == cp.MODE_SHADOW
    assert shadow_result["promoted"] == 0
    assert calls == [], "shadow 态绝不能触达写路径的专用入口"

    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    on_result = await cp.run_promotion_pass(db_session)
    assert on_result["promoted"] == 2
    assert len(calls) == 1, "on 态必须、且只能经这一个入口进候选集"


@pytest.mark.anyio
async def test_write_path_ids_come_from_dedicated_entrypoint_not_shadow_list(
        db_session, monkeypatch):
    """把 select_promotions_for_write 换成返回真子集的桩，证明喂给
    grant_citizenship_batch 的 id 就是这个专用入口的返回值——不是从
    select_promotions() 的默认（shadow-defaulting）调用算出的候选表旁路
    进来的（世界里有 2 个合法候选，桩只放行 1 个，DB 必须只写 1 行）。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    _, us = await _world(db_session, denizens=2)
    only_first = (us[0].id,)
    monkeypatch.setattr(cp, "select_promotions_for_write",
                        lambda *a, **kw: only_first)

    result = await cp.run_promotion_pass(db_session)
    assert result["promoted"] == 1
    rows = (await db_session.execute(
        select(CivicStandingHistory))).scalars().all()
    assert len(rows) == 1
    assert rows[0].resident_id == only_first[0]


# ── 写路径与探针（Task 11）的接线 ─────────────────────────────────────────

@pytest.mark.anyio
async def test_on_mode_writes_history_shaped_for_the_probes_leak_check(
        db_session, monkeypatch):
    """写路径产生的历史行必须让 Task 11 探针（burnin_report.py）的泄漏判据
    认出「合法晋升」——显式跑一遍探针而不是假设写形状正确。"""
    from scripts.burnin_report import fetch_civic_standing_snapshot

    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    await _world(db_session, denizens=2)

    result = await cp.run_promotion_pass(db_session)
    assert result["promoted"] == 2

    snapshot = await fetch_civic_standing_snapshot(db_session)
    assert snapshot["available"] is True
    assert snapshot["leaked"] == []
    assert snapshot["cross"]["ugc_citizen_promoted"] == 2
    assert snapshot["cross"]["ugc_citizen_unrecorded"] == 0


# ── 运行摘要的列宽预算（复审 Minor 1，因生产是 Postgres 而升级）───────────
#
# 旧实现按 slug **个数**截断到 50——``Resident.slug`` 是 ``String(100)``，
# 50 × 100 + JSON 开销 ≈ 5 KB，稳超 ``SystemConfig.value`` 的 ``String(2000)``。
# Postgres 上会 ``StringDataRightTruncation``；``_record_run`` 是 fail-open，
# 会把那次异常整个吞掉只留一条 warning——shadow 三夜观察期的名单就无声无息
# 地从没写进去过。SQLite 本地测试不校验 VARCHAR 列宽，测不出来，所以下面的
# 断言直接量序列化后的字符数，不指望数据库报错。

def _worst_case_slugs(n: int) -> list[str]:
    """``Resident.slug`` 的列宽上限是 ``String(100)``——每个 slug 精确
    100 字符，模拟最坏情形。"""
    return [str(i).zfill(4).ljust(100, "x") for i in range(n)]


def test_bounded_summary_payload_fits_within_the_system_config_column():
    result = {
        "mode": cp.MODE_SHADOW,
        "world_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
        "citizens_before": 11,
        "candidates": _worst_case_slugs(300),
        "promoted": 0,
        "refused": None,
    }
    payload = cp._bounded_summary_payload(result)
    serialized = json.dumps(payload)

    assert len(serialized) <= 2000
    # candidate_count 是真实候选总数，不受截断影响——读者要能分清「刚好 50
    # 个候选」和「300 个候选只看到一部分」。
    assert payload["candidate_count"] == 300
    assert payload["candidates_truncated"] is True
    assert len(payload["candidates"]) < 300
    assert payload["candidates"] == _worst_case_slugs(300)[:len(payload["candidates"])]


def test_bounded_summary_payload_does_not_mark_a_small_list_truncated():
    """没撞预算时不能误报"被砍过"。"""
    result = {
        "mode": cp.MODE_SHADOW,
        "world_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
        "citizens_before": 11,
        "candidates": ["u0", "u1"],
        "promoted": 0,
        "refused": None,
    }
    payload = cp._bounded_summary_payload(result)
    assert payload["candidates"] == ["u0", "u1"]
    assert payload["candidates_truncated"] is False
    assert payload["candidate_count"] == 2
    # 没有 refused_detail 时（没有发生拒绝）也不能误报"被砍过"。
    assert payload["refused_detail_truncated"] is False
    assert payload["refused_detail_original_length"] is None


def test_bounded_summary_payload_also_bounds_the_refused_detail_text():
    """CivicStandingRefused 的异常消息是自由文本，理论上可以任意长——不能
    让它反过来把 candidates 的预算全部挤掉。"""
    result = {
        "mode": cp.MODE_ON,
        "world_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
        "citizens_before": 11,
        "candidates": _worst_case_slugs(20),
        "promoted": 0,
        "refused": "grant_refused",
        "refused_detail": "x" * 5000,
    }
    payload = cp._bounded_summary_payload(result)
    serialized = json.dumps(payload)
    assert len(serialized) <= 2000
    assert len(payload["refused_detail"]) < 5000


@pytest.mark.anyio
async def test_record_run_persists_the_bounded_payload_via_the_dedicated_session(
        db_session):
    """把 Important 2（独立 session）与 Minor 1（预算截断）接在一起验证：
    即使 result 里塞进了会撑爆列宽的候选名单，_record_run 落库的还是能被
    ConfigService 读回来的合法 JSON，而不是被 fail-open 悄悄吞掉。"""
    result = {
        "mode": cp.MODE_SHADOW,
        "world_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
        "citizens_before": 11,
        "candidates": _worst_case_slugs(300),
        "promoted": 0,
        "refused": None,
    }
    await cp._record_run(db_session, result)

    summary = await ConfigService(db_session).get(cp.RUN_SUMMARY_KEY)
    assert summary is not None, "预算截断不该让 fail-open 把整次写都吞掉"
    assert summary["candidate_count"] == 300
    assert summary["candidates_truncated"] is True
    assert len(summary["candidates"]) < 300


# ── 二次复审 Minor 1-4 ────────────────────────────────────────────────────
#
# 四项都发生在 _record_run / _bounded_summary_payload 附近，测试自然是同一
# 个套件——按复审说明合并成一组 red/green，不拆四对 commit。

def test_bounded_summary_payload_bounds_a_worst_case_chinese_refused_detail():
    """复审 Minor 1（F3 那个洞在拒绝路径上重演）：``_REFUSED_DETAIL_MAX_
    CHARS = 300`` 截的是原串字符数，但 ``ensure_ascii=True`` 把每个非 ASCII
    字符展开成 6 字符的 ``\\uXXXX`` 转义。300 个中文字符编码后是 1800 字符，
    candidates 已经空了（这里干脆传 ``[]``）时序列化后仍然超预算——本仓的
    异常消息习惯用中文（``_assert_revocable`` / ``assert_thresholds_
    calibrated`` 都是长中文串），F1(b) 的前提是"任何未来的拒绝类都不能让
    摘要消失"，不能只挡住今天的英文候选名单场景。"""
    result = {
        "mode": cp.MODE_ON,
        "world_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
        "citizens_before": 11,
        "candidates": [],
        "promoted": 0,
        "refused": "grant_refused",
        "refused_detail": "拒" * 5000,
    }
    payload = cp._bounded_summary_payload(result)
    serialized = json.dumps(payload)
    assert len(serialized) <= 2000
    assert payload["refused_detail"] is None or len(payload["refused_detail"]) < 300
    # 三次复审：两轮截断（先按 _REFUSED_DETAIL_MAX_CHARS 原始字符裁，再按
    # 序列化预算逐字符裁）都对同一份 5000 字符输入生效，标记必须是 True。
    assert payload["refused_detail_truncated"] is True
    assert payload["refused_detail_original_length"] == 5000


# ── 三次复审：refused_detail 也要有对称的截断标记 ─────────────────────────
#
# candidates 有 candidates_truncated；refused_detail 的两轮截断（原始字符
# 裁 _REFUSED_DETAIL_MAX_CHARS / 序列化预算裁）都没有等价标记——如果原始
# 异常消息恰好落在裁剪后的长度上，payload 长得一模一样，读者分不清"完整"
# 还是"被砍过"。两条独立的裁剪路径都要能各自把标记置位，不能只顾一条。

def test_bounded_summary_payload_flags_the_raw_char_cap_cut_on_refused_detail():
    """只触发第一轮裁剪（原始字符数 > _REFUSED_DETAIL_MAX_CHARS=300），
    序列化后的结果本身不撞预算——ASCII 字符不会被 ensure_ascii 展开，300
    个字符编码后还是 300 字符左右，远低于 2000。"""
    result = {
        "mode": cp.MODE_ON,
        "world_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
        "citizens_before": 11,
        "candidates": [],
        "promoted": 0,
        "refused": "grant_refused",
        "refused_detail": "x" * 5000,
    }
    payload = cp._bounded_summary_payload(result)
    assert len(json.dumps(payload)) <= 2000
    assert len(payload["refused_detail"]) == 300
    assert payload["refused_detail_truncated"] is True
    assert payload["refused_detail_original_length"] == 5000


def test_bounded_summary_payload_flags_the_serialized_budget_cut_on_refused_detail():
    """只触发第二轮裁剪：原始字符数 ≤ 300（第一轮不动它），但
    ``ensure_ascii=True`` 把中文字符展开成 ``\\uXXXX`` 后序列化仍然超预算
    （290 个中文字符编码后 ≈ 1740 字符，加上其它字段 > 2000；实测过
    290 → 2028 字符）。这条路径专门证明：即使第一轮判定"没超原始字符上限"
    ，第二轮仍然可能要裁，且必须一样把标记置位。"""
    original = "拒" * 290
    result = {
        "mode": cp.MODE_ON,
        "world_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
        "citizens_before": 11,
        "candidates": [],
        "promoted": 0,
        "refused": "grant_refused",
        "refused_detail": original,
    }
    payload = cp._bounded_summary_payload(result)
    assert len(json.dumps(payload)) <= 2000
    assert payload["refused_detail_original_length"] == 290
    # 第一轮（_REFUSED_DETAIL_MAX_CHARS=300）不会动它——290 ≤ 300
    assert len(payload["refused_detail"]) < 290, (
        "这条用例要测的是第二轮（序列化预算）裁剪；如果长度没变，说明 290 "
        "个中文字符编码后其实没有撞预算，用例前提就不成立了")
    assert payload["refused_detail_truncated"] is True


def test_bounded_summary_payload_does_not_flag_an_untruncated_refused_detail():
    """两轮裁剪都没碰到的正常情形——今天真实的化身拒绝消息就是这个量级
    （序列化后 576 字符，参见附 6.3）——不能误报"被砍过"，
    refused_detail_original_length 要能验证"确实原样保留"。"""
    original = "grant refused: 1 target(s) are player avatars (users.player_resident_id hits: ['avatar'])"
    result = {
        "mode": cp.MODE_ON,
        "world_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
        "citizens_before": 11,
        "candidates": ["u0", "u1"],
        "promoted": 0,
        "refused": "grant_refused",
        "refused_detail": original,
    }
    payload = cp._bounded_summary_payload(result)
    assert payload["refused_detail"] == original
    assert payload["refused_detail_truncated"] is False
    assert payload["refused_detail_original_length"] == len(original)


@pytest.mark.anyio
async def test_pool_exhaustion_is_logged_at_error_not_generic_warning(
        tmp_path, caplog):
    """复审 Minor 2：``_record_run`` 期间同时占用 2 条连接（调用方 db 一条 +
    专用 session 一条）。池耗尽时 checkout 会等到 ``pool_timeout`` 才抛超时
    ——落进通用 fail-open 会和"没什么好记的"共用同一条 warning，运维分不清
    这次是真的没数据还是连接池忙不过来。用小池（``pool_size=1,
    max_overflow=0``）复现：先用一个 session 占住唯一的连接（flush 但不
    commit/close），再跑 ``_record_run``——它自己需要的第二条连接永远拿
    不到。"""
    from sqlalchemy.ext.asyncio import (
        AsyncSession as _AsyncSession, async_sessionmaker as _async_sessionmaker,
        create_async_engine as _create_async_engine,
    )

    from app.database import Base as _Base

    engine = _create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'pool_exhaustion.db'}",
        pool_size=1, max_overflow=0, pool_timeout=1)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    factory = _async_sessionmaker(engine, class_=_AsyncSession,
                                  expire_on_commit=False, autoflush=False)

    holder = factory()
    holder.add(_res("holder_row", cm.UGC_RESIDENT_TYPE))
    await holder.flush()   # 占住这个池唯一的一条连接，不提交也不关闭

    caplog.set_level("ERROR", logger="app.tasks.civic_promotion")
    result = {
        "mode": cp.MODE_SHADOW,
        "world_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
        "citizens_before": 1, "candidates": ["u0"], "promoted": 0,
        "refused": None,
    }
    await cp._record_run(holder, result)   # 不该抛异常——fail-open

    await holder.rollback()
    await holder.close()
    await engine.dispose()

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert error_records, (
        "连接池耗尽必须单独用 ERROR 记，不能和通用 fail-open 的 warning "
        "共用同一条日志")
    assert any("pool" in r.getMessage().lower()
              or "连接" in r.getMessage() for r in error_records), (
        "ERROR 日志必须点名是连接池问题，不能只是通用的"
        "「recording ... failed」")


@pytest.mark.anyio
async def test_record_run_never_disposes_the_shared_connection_pool(
        db_session, monkeypatch):
    """复审 Minor 3：``AsyncEngine(db.get_bind())`` 包住的是与 ``db`` 同一个
    连接池（已用脚本验证过 ``wrapped.pool is engine.pool``）。这个 wrapped
    engine 绝不能调用 ``.dispose()``——那会把整个应用共用的连接池连根拔起，
    不是关掉这一次专用 session 自己的什么东西。用 spy 顶替
    ``AsyncEngine.dispose``，跑几次 ``_record_run``，断言从未被调用；再确认
    调用方的 session 之后仍然可用（池没被拆）。"""
    from sqlalchemy.ext.asyncio import AsyncEngine

    calls = []
    real_dispose = AsyncEngine.dispose

    async def _spy_dispose(self, *args, **kwargs):
        calls.append(self)
        return await real_dispose(self, *args, **kwargs)

    monkeypatch.setattr(AsyncEngine, "dispose", _spy_dispose)

    for _ in range(3):
        await cp._record_run(db_session, {
            "mode": cp.MODE_SHADOW,
            "world_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
            "citizens_before": 1, "candidates": ["u0"], "promoted": 0,
            "refused": None,
        })

    assert calls == [], "_record_run 的 scratch engine 绝不能调用 dispose()"

    # 池没被拆——调用方的 session 之后仍然能正常查询
    summary = await ConfigService(db_session).get(cp.RUN_SUMMARY_KEY)
    assert summary is not None


@pytest.mark.anyio
async def test_record_run_refuses_a_connection_bound_session_instead_of_silently_losing_data(
        db_engine, caplog):
    """复审 Minor 4：``AsyncSession.get_bind()`` 的返回类型是
    ``Engine | Connection``。实测过（见任务报告）：``AsyncEngine(connection)``
    **不会**抛异常，而是悄悄包出一个复用同一条连接/同一个事务的坏
    wrapper——比"抛异常被 fail-open 吞掉"更隐蔽，因为连异常都没有，只是
    数据对不上（Important 2 那个漏洞的另一种变种）。这里显式判
    ``isinstance(bind, Engine)``，不满足就直接拒绝、单独告警。"""
    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

    caplog.set_level("ERROR", logger="app.tasks.civic_promotion")
    async with db_engine.connect() as conn:
        conn_bound = _AsyncSession(bind=conn, expire_on_commit=False)
        result = {
            "mode": cp.MODE_SHADOW,
            "world_at": datetime(2026, 8, 1, tzinfo=UTC).isoformat(),
            "citizens_before": 1, "candidates": ["u0"], "promoted": 0,
            "refused": None,
        }
        await cp._record_run(conn_bound, result)   # 不该抛异常
        await conn_bound.close()

    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert any("engine" in r.getMessage().lower() for r in error_records), (
        "Connection-bound session 必须单独用 ERROR 记，不能悄悄走 "
        "AsyncEngine(connection) 那条会复用同一事务的坏路")


# ── 数值闸门 ───────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_circuit_breaker_refuses_the_whole_batch(db_session, monkeypatch):
    """候选集 > max(下限 3, 当前公民数 × 20%) → 整批拒绝并告警，**不截断**。
    截断会掩盖「阈值写反」这类全量误判。

    世界规模刻意开到 20 位内置公民，让**比例项**（20 × 0.20 = 4）压过绝对下限
    （3）——这样本用例测的是比例语义，不是下限语义。5 > 4 → 熔断。
    """
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    monkeypatch.setenv("CIVIC_PROMOTION_MAX_PER_RUN", "100")
    await _world(db_session, builtins=20, denizens=5)   # 5 > max(3, 20 × 0.20)

    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] == "circuit_breaker"
    assert result["promoted"] == 0
    assert len(result["candidates"]) == 5
    assert (await db_session.execute(
        select(func.count()).select_from(CivicStandingHistory))).scalar() == 0


@pytest.mark.anyio
async def test_breaker_floor_keeps_a_small_town_promotable(db_session, monkeypatch):
    """熔断的绝对下限：小镇规模下比例项 < 3 时以下限为准，合法小批量照常放行。

    没有下限的话，4 位内置公民 × 0.20 = 0.8，**一个候选都过不去**——熔断恒响、
    单夜上限恒不生效，两道闸门语义互相吞掉（生产 11 位公民时阈值 ≈2.2，
    MAX_PER_RUN=5 永远够不着）。
    """
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    await _world(db_session, builtins=4, denizens=3)    # 3 ≤ max(3, 0.8)

    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] is None
    assert result["promoted"] == 3


@pytest.mark.anyio
async def test_breaker_floor_can_be_disabled(db_session, monkeypatch):
    """置 0 即退化成纯比例判定（世界规模足够大之后的口径）。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_MIN_ABS", "0")
    await _world(db_session, builtins=4, denizens=3)    # 3 > 4 × 0.20 = 0.8

    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] == "circuit_breaker"
    assert result["promoted"] == 0


@pytest.mark.anyio
async def test_max_per_run_truncates_deterministically(db_session, monkeypatch):
    """单夜上限是确定性截断（候选已按 id 排序），余量下夜再来——整批拒绝会让
    合法积压永久卡死。截断发生在熔断判定**之后**。"""
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    monkeypatch.setenv("CIVIC_PROMOTION_MAX_PER_RUN", "2")
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_FRACTION", "10")   # 熔断不挡
    await _world(db_session, builtins=4, denizens=3)

    first = await cp.run_promotion_pass(db_session)
    assert first["promoted"] == 2
    assert len(first["candidates"]) == 3
    assert first["refused"] is None
    second = await cp.run_promotion_pass(db_session)
    assert second["promoted"] == 1


@pytest.mark.anyio
async def test_breaker_is_evaluated_on_the_full_candidate_set(db_session,
                                                              monkeypatch):
    """先熔断再截断。反过来会让熔断永远打不响（截断后的集合恒 ≤ 上限）。

    同上，世界开到 20 位内置公民让比例项（4）压过绝对下限（3）：5 个候选被
    cap=1 截断后只剩 1 个，若顺序反了就永远 1 ≤ 4，熔断这辈子打不响。
    """
    monkeypatch.setenv("CIVIC_PROMOTION_MODE", "on")
    monkeypatch.setenv("CIVIC_PROMOTION_MAX_PER_RUN", "1")
    monkeypatch.setenv("CIVIC_PROMOTION_BREAKER_FRACTION", "0.20")
    await _world(db_session, builtins=20, denizens=5)

    result = await cp.run_promotion_pass(db_session)
    assert result["refused"] == "circuit_breaker"
    assert result["promoted"] == 0


# ── 接线已在收口完成 ───────────────────────────────────────────────────

def test_the_cron_is_now_wired():
    """**改的是规格，不是为了让它绿。**

    这条原本叫 `test_this_line_does_not_wire_the_cron`，断言的是
    `"civic_promotion" not in nightly_cron.py`——它守的是 F2 开发期的一条**过程
    约束**（共享文件线内不改，接线延到收口 §8 第 2 项），不是产品行为。收口在
    2026-07-28 完成后，原断言的前提消失：它再红就只是在报告「收口做完了」。

    位置约束（close_due_polls 之后、run_npc_voting 之前）是真正需要长期守住的
    那一半，已迁到 `tests/test_nightly_civic_promotion_wiring.py`，那里按调用
    次序做断言，比字符串包含检查强。这里只保留「确实接上了」这一条。
    """
    src = (BACKEND_ROOT / "app" / "tasks" / "nightly_cron.py").read_text(
        encoding="utf-8")
    assert "run_promotion_pass" in src, (
        "收口第 2 项已完成，nightly_cron.py 必须调用 run_promotion_pass；"
        "位置断言见 tests/test_nightly_civic_promotion_wiring.py")
