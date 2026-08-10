"""W2 —— 候选池里的 world_event 专用道(``REALISM_POOL_WORLD_EVENT_RESERVE``)。

第 2 段(``test_pool_reserved_slots.py``)给镇务结果档开了第一条道。本段并上第二条::

    pool(cap) = civic道(≤ civic_reserve) ∪ world_event道(≤ we_reserve)
              ∪ 个人臂(cap − 两道**实拿**之和)

**为什么不能按 source 直接开道**(2026-08-10 生产只读实测):

    source='world_event' 且带 raw_importance 的记忆   0 / 1380
    近 3 天 world_event 记忆构成                       weather 261 / festival 17
    公共臂 top-41(第 2 段对拍)                       全是 importance=0.5 的天气

按 ``source='world_event'`` + ``created_at DESC`` 开道 → **94% 抓到天气**。所以这条
道认的是 W1 落下的显式档位标记 ``metadata_json->>'tier' = 'substantive'``,
``tier='trivia'`` 与**存量那 1380 条没有这个键的行**都收不到(存量不回填:数据变更
与开闸不同车)。

**设计反转**(本段最重要的判断):有了保留位之后,「分档抬 importance」那套机制既
不必要也不该用 —— 抬到 0.99 的实质事件会**同时**挤占个人臂(12 周饱和:day28 7/30
→ day84 21/30)**又**吃专用道坑位,双重占坑;而专用道本身就保证检索得到。所以本文件
播种的实质世界事件一律是**低 importance 的直写形状**(0.5/0.6,与天气同档),它们能
进池**只**因为专用道,不因为排序 —— 每条用例都配一句 ``we_reserve=0`` 时它们进不去
的对照,否则「进池了」这句话可以由一个恒真的池子说出来。

第 2 段的四条不变量原样适用,且必须对**两条道同时**成立:

1. ``len(pool)`` 恒等于 ``min(cap, 活跃 event 总数)`` —— 两条道各自没填满的坑都要
   退还个人臂。个人臂的 limit 是 ``cap − (civic实拿 + we实拿)``,用**实拿条数**不是
   reserve 配置值;
2. 两条道的成员都从个人臂排除,**且两条道之间不重叠**(source 互斥,但要有断言钉住
   —— 哪天有人把 civic 道的判据放宽成「公共通道」,重叠就发生了);
3. 合并后仍按 ``importance DESC, created_at DESC`` 交给打分层;
4. ``cap < POOL_RESERVE_MIN_CAP``(fail-open 路径)时**两条道都不生效**。

``we_reserve=0`` 的逐字节对拍照第 2 段的**双轨**范式写,对的是**第 2 段的行为**
(不是 master 的、也不是保留位落地之前的):

- **轨 1(常驻)**:第 2 段那段候选池实现在本文件里冻结成
  ``_frozen_pre_we_candidates``。纯 SQLAlchemy,不碰 git,**与克隆深度无关**——
  CI 的 ``actions/checkout@v4`` 默认 ``fetch-depth: 1``,任何依赖历史 ref 的对拍在
  那里都取不到对象;
- **轨 2(加强,拿得到才跑)**:把 ``6db4c3c``(第 2 段的合并提交 = 本分支的起点)
  那份真实 ``service.py`` 装成独立模块三方对拍,顺带钉住轨 1 没抄漂。ref 用**固定
  SHA** 而不是会随合并漂移的 ``master``;浅克隆里取不到就 skip。
"""
import functools
import importlib.util
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.memory import service as service_mod
from app.memory.service import MemoryService
from app.models.memory import Memory
from app.services import world_event_service as wes
from tests.test_pool_reserved_slots import (
    _POOL_CAP, _SATURATED, _check_invariants, _civic_of, _personal_of,
    _resident, _seed_civic, _seed_personal)

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 实质世界事件的落库 importance。**刻意与天气同档**(直写那条路的随机样本档) ——
#: 设计反转之后实质事件不再抬 importance,进池全靠专用道。这个数低于饱和史池底
#: (0.995)就是「靠排序永远进不去」的复现条件。
_WE_IMPORTANCE = 0.5


# ── 轨 1:第 2 段实现的冻结快照(常驻,不依赖 git 历史)──────────────────

def _frozen_pool_order_key(m: Memory):
    """``6db4c3c`` 的 ``_pool_order_key`` 逐字快照。"""
    created = m.created_at
    if created is None:
        return (m.importance or 0.0, datetime.min.replace(tzinfo=UTC))
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (m.importance or 0.0, created)


async def _frozen_pre_we_candidates(db, resident_id: str, cap: int) -> list[Memory]:
    """``6db4c3c:backend/app/memory/service.py`` 里 ``_fetch_event_candidates``
    (**一条道**的那一版)+ 它调用的两条查询,逐字抄写成一个自足的函数。

    为什么整段抄而不是转调 ``self._fetch_reserved_civic_candidates`` /
    ``_fetch_personal_candidates``:转调是自指 —— 哪天那两条查询被改了,快照跟着
    改,「与第 2 段逐字节相同」这句承诺就由被测对象自己证明自己。

    为什么要冻结:CI 的 ``actions/checkout@v4`` 默认 ``fetch-depth: 1``,浅克隆里
    既没有 ``master`` 这个 ref 也没有 ``6db4c3c`` 这个对象 —— 任何 ``git show``
    对拍在那里要么整片红,要么只能 skip 成假绿。这份快照是纯 SQLAlchemy,
    **与克隆深度无关、永远会跑**。它有没有抄漂由轨 2 在有 git 的环境里钉住。
    """
    reserve = settings.realism_pool_civic_reserve
    effective_reserve = 0 if cap < service_mod.POOL_RESERVE_MIN_CAP else max(reserve, 0)
    effective_reserve = min(effective_reserve, cap)
    if effective_reserve <= 0:
        reserved: list[Memory] = []
    else:
        reserved = list((await db.execute(
            select(Memory)
            .where(
                Memory.resident_id == resident_id,
                Memory.type == "event",
                Memory.archived_at.is_(None),
                Memory.metadata_json["civic_event"].as_string().like(
                    f"{service_mod.CIVIC_RESULT_EVENT_PREFIX}%"),
            )
            .order_by(Memory.created_at.desc())
            .limit(effective_reserve)
        )).scalars().all())

    stmt = (
        select(Memory)
        .where(
            Memory.resident_id == resident_id,
            Memory.type == "event",
            Memory.archived_at.is_(None),
        )
    )
    if reserved:
        stmt = stmt.where(Memory.id.notin_([m.id for m in reserved]))
    stmt = (
        stmt
        .order_by(Memory.importance.desc(), Memory.created_at.desc())
        .limit(cap - len(reserved))
    )
    personal = list((await db.execute(stmt)).scalars().all())
    if not reserved:
        return personal
    return sorted(reserved + personal, key=_frozen_pool_order_key, reverse=True)


# ── 轨 2:钉死 SHA 的 git 对拍(拿得到就跑,拿不到就 skip)────────────────

#: 第 2 段的合并提交 = 本分支的起点 —— **world_event 专用道落地之前**的那棵树。
#:
#: 钉 SHA 不钉 ``master``:``master`` 会随本批合入而漂,漂完之后「对拍第 2 段」对的
#: 就是它自己,下面那条防恒绿的守卫会当场红(第 2 段踩过一次,基线 54→63)。
_PRE_WE_SHA = "6db4c3cdbe837b1d2a874afc63a67ee04ba65f53"

_TRACK1_IS_THE_REAL_GUARD = (
    "轨 2(git 对拍 {sha})只是**加强**:{why}。"
    "真正的保险是轨 1 —— 本文件里的 ``_frozen_pre_we_candidates`` 冻结快照,"
    "它不碰 git、与克隆深度无关、每一次都真的跑,同样 8 组入参逐条对 id 序列。"
    "所以这里 skip 不代表「第 2 段的行为没人守」。"
)


@functools.lru_cache(maxsize=1)
def _pinned_ref_source() -> str | None:
    """取 ``_PRE_WE_SHA`` 那份 ``service.py``;取不到返回 ``None``。

    取不到的正当情形:CI 的 ``fetch-depth: 1`` 浅克隆(对象根本没下载)、导出成
    tarball 的源码树、没有 git 的机器。这些都不该让整片测试红。
    """
    try:
        proc = subprocess.run(
            ["git", "show", f"{_PRE_WE_SHA}:backend/app/memory/service.py"],
            cwd=_REPO_ROOT, capture_output=True, text=True)
    except OSError:
        return None
    return proc.stdout if proc.returncode == 0 else None


def _pinned_ref_memory_service():
    """把第 2 段那份 ``service.py`` 装成独立模块;拿不到就 skip(文案见上)。"""
    src = _pinned_ref_source()
    if src is None:
        pytest.skip(_TRACK1_IS_THE_REAL_GUARD.format(
            sha=_PRE_WE_SHA[:7],
            why="这个仓里取不到那个对象(浅克隆 / 无 git / 源码 tarball)"))
    assert "realism_pool_world_event_reserve" not in src, (
        f"从 {_PRE_WE_SHA[:7]} 取到的 service.py 里已经有 world_event 专用道了 ——"
        "对拍对的是它自己,这条断言恒绿。SHA 钉错了(应为本段落地**之前**的起点)。")
    assert "realism_pool_civic_reserve" in src, (
        f"从 {_PRE_WE_SHA[:7]} 取到的 service.py 里**没有**第 2 段的镇务保留位 ——"
        "SHA 钉到了第 2 段之前,对拍对的就不是「第 2 段的行为」了。")
    name = f"_we_lane_ref_service_{_PRE_WE_SHA[:7]}"
    if name in sys.modules:
        return sys.modules[name].MemoryService
    path = Path(tempfile.gettempdir()) / f"{name}.py"
    path.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.MemoryService


# ── 播种 ────────────────────────────────────────────────────────────────

async def _seed_world_events(db, rid: str, n: int, *, tier: str | None,
                             importance: float = _WE_IMPORTANCE) -> list[Memory]:
    """世界事件记忆,下标 0 最新(每条相隔一分钟,全都比个人史新)。

    ``tier=None`` = **存量形状**:1380 条老记忆的 metadata 里根本没有这个键,专用道
    对它们查不到 —— 这是预期的(本批不回填)。
    """
    now = datetime.now(UTC)
    out = []
    for i in range(n):
        meta = {"first_hand": True, "event_id": f"we-{tier}-{i}"}
        if tier is not None:
            meta["tier"] = tier
        mem = Memory(resident_id=rid, type="event", content=f"镇上的事 {tier} {i}",
                     importance=importance, source="world_event", metadata_json=meta)
        mem.created_at = now - timedelta(minutes=i + 1)
        db.add(mem)
        out.append(mem)
    await db.commit()
    return out


@pytest.fixture
def reserves(monkeypatch):
    """两条道的闸位一起设 —— 本段的每一笔账都是**两条道加起来**的账。"""
    def _set(civic: int, we: int):
        monkeypatch.setattr(settings, "realism_pool_civic_reserve", civic)
        monkeypatch.setattr(settings, "realism_pool_world_event_reserve", we)
    return _set


def _we_of(pool: list[Memory]) -> list[Memory]:
    return [m for m in pool if m.source == "world_event"]


# ── 旋钮 ────────────────────────────────────────────────────────────────

def test_the_world_event_reserve_knob_defaults_to_closed():
    """``0 = 逐字节等于第 2 段``。一个数同时表达「开没开」与「几个坑」,默认必须是 0
    —— 开闸是**单独一次**部署变更(红线:行为开闸与代码变更不同车)。"""
    from app.config import Settings
    assert "realism_pool_world_event_reserve" in Settings.model_fields, \
        "旋钮不在 Settings 里,REALISM_ 前缀的 env 会被拒"
    assert Settings.model_fields["realism_pool_world_event_reserve"].default == 0


def test_the_lane_marker_is_the_same_literal_the_writer_stamps():
    """检索侧认的档位标记与写入侧盖的必须是同一个字面量。

    两处各写一份字符串是本仓既有做法(``CIVIC_RESULT_EVENT_PREFIX`` 同样),代价是
    可以无声漂移 —— 漂了以后专用道查不到任何东西,而所有「没被收」的断言照旧全绿。
    这条把两侧钉在一起。
    """
    assert service_mod.SUBSTANTIVE_TIER == wes.TIER_SUBSTANTIVE
    assert service_mod.WORLD_EVENT_SOURCE == "world_event"
    assert wes.TIER_TRIVIA != wes.TIER_SUBSTANTIVE


def test_the_lane_predicate_compiles_on_both_dialects():
    """K17:测试跑 sqlite,生产跑 PG —— JSON 路径必须两边都编得出来。

    JSON 路径是本仓历史上唯一一处「sqlite 绿、PG 炸」的形态,所以这条静态断言把两个
    方言的产物都钉住:PG 走 ``->>``,sqlite 走 ``JSON_EXTRACT``。
    """
    from sqlalchemy import select as _select
    from sqlalchemy.dialects import postgresql, sqlite

    stmt = _select(Memory.id).where(
        Memory.source == service_mod.WORLD_EVENT_SOURCE,
        Memory.metadata_json["tier"].as_string() == service_mod.SUBSTANTIVE_TIER)

    def _sql(dialect) -> str:
        return str(stmt.compile(dialect=dialect,
                                compile_kwargs={"literal_binds": True}))

    pg, lite = _sql(postgresql.dialect()), _sql(sqlite.dialect())
    assert "metadata_json ->> 'tier'" in pg and "'substantive'" in pg, pg
    assert "memories.source = 'world_event'" in pg, pg
    assert "JSON_EXTRACT(memories.metadata_json, '$.\"tier\"') = 'substantive'" in lite, lite
    assert "memories.source = 'world_event'" in lite, lite


# ── ①we_reserve=0:逐字节等于第 2 段 ──────────────────────────────────

#: 两轨共用的 8 组入参。``(个人史, 镇务 refs, 实质 we, 琐事 we, 存量 we,
#: civic_reserve, cap)`` —— 每组都同时铺三种 world_event 形状,因为「we 道关着」
#: 要对的正是「这三种一条都不许被多收进来」。
_PARITY_CASES = [
    # 生产饱和形状 × 两条结果记忆 × 三种世界事件都在 —— 开闸后差别最大的那组
    pytest.param(_SATURATED, ["poll_result:p-1", "poll_result:p-2"], 5, 5, 5, 2, 30,
                 id="saturated-civic-open"),
    # 第 2 段的闸也关着:两条道全关 = 一条查询的那条旧路径
    pytest.param(_SATURATED, ["poll_result:p-1", "poll_result:p-2"], 5, 5, 5, 0, 30,
                 id="saturated-both-closed"),
    # 一条实质世界事件都没有(绝大多数时刻的形状)
    pytest.param(_SATURATED, ["poll_result:p-1"], 0, 40, 0, 2, 30, id="only-weather"),
    # 存量形状:1380 条老记忆一律没有 tier 键
    pytest.param(_SATURATED, [], 0, 0, 40, 2, 30, id="legacy-only"),
    # 新居民:总数不足 cap
    pytest.param([0.6] * 12, ["poll_result:p-1"], 3, 0, 0, 2, 30, id="new-resident"),
    # 空史
    pytest.param([], [], 0, 0, 0, 2, 30, id="empty"),
    # 非 30 的 cap:fail-open 那条(10)、比池深还大的(50)
    pytest.param(_SATURATED, ["poll_result:p-1"], 5, 5, 5, 2, 10, id="cap-10"),
    pytest.param(_SATURATED, ["poll_result:p-1"], 5, 5, 5, 2, 50, id="cap-50"),
]


async def _seed_parity_case(db, history, civic_refs, we_sub, we_trivia, we_legacy) -> str:
    rid = await _resident(db)
    await _seed_personal(db, rid, history)
    await _seed_civic(db, rid, civic_refs)
    await _seed_world_events(db, rid, we_sub, tier="substantive")
    await _seed_world_events(db, rid, we_trivia, tier="trivia")
    await _seed_world_events(db, rid, we_legacy, tier=None)
    return rid


@pytest.mark.anyio
@pytest.mark.parametrize(
    "history,civic_refs,we_sub,we_trivia,we_legacy,civic_reserve,cap", _PARITY_CASES)
async def test_we_reserve_zero_returns_the_exact_stage2_sequence(
        db_session, reserves, history, civic_refs, we_sub, we_trivia, we_legacy,
        civic_reserve, cap):
    """**轨 1(常驻)**:``we_reserve=0`` 逐字节等于第 2 段,对拍冻结快照。

    对拍的是 **id 序列全等**,不是长度相等 —— 长度相等的两个池完全可能装着不同的
    记忆,而 0 承诺的是「逐字节旧行为」不是「同样多」。

    这条是本批的回滚保险:部署先以 ``we_reserve=0`` 上线(零迁移),开闸是**单独一次
    变更**。若 0 这个值本身已经改变了行为,那次「安全部署」就不安全了。
    """
    reserves(civic_reserve, 0)
    rid = await _seed_parity_case(
        db_session, history, civic_refs, we_sub, we_trivia, we_legacy)

    mine = await MemoryService(db_session)._fetch_event_candidates(rid, cap=cap)
    frozen = await _frozen_pre_we_candidates(db_session, rid, cap)

    assert [m.id for m in mine] == [m.id for m in frozen]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "history,civic_refs,we_sub,we_trivia,we_legacy,civic_reserve,cap", _PARITY_CASES)
async def test_we_reserve_zero_matches_the_pinned_stage2_implementation(
        db_session, reserves, history, civic_refs, we_sub, we_trivia, we_legacy,
        civic_reserve, cap):
    """**轨 2(加强)**:同样 8 组入参,参照物换成 ``6db4c3c`` 那份**真实**
    ``service.py``(装成独立模块跑真 SQL)。

    它比轨 1 多证一件事:轨 1 的冻结快照**没有抄漂**。所以这里三方对拍 —— 新实现
    == 第 2 段真实实现,且冻结快照 == 第 2 段真实实现。拿不到那个对象时(浅克隆)
    整条 skip;skip 不留缺口,因为轨 1 覆盖同一批入参、永远会跑。
    """
    ref_cls = _pinned_ref_memory_service()   # 拿不到就在这里 skip
    reserves(civic_reserve, 0)
    rid = await _seed_parity_case(
        db_session, history, civic_refs, we_sub, we_trivia, we_legacy)

    mine = await MemoryService(db_session)._fetch_event_candidates(rid, cap=cap)
    theirs = await ref_cls(db_session)._fetch_event_candidates(rid, cap=cap)
    frozen = await _frozen_pre_we_candidates(db_session, rid, cap)

    assert [m.id for m in mine] == [m.id for m in theirs]
    assert [m.id for m in frozen] == [m.id for m in theirs], \
        f"轨 1 的冻结快照与 {_PRE_WE_SHA[:7]} 的真实实现漂了 —— 快照要重抄"


# ── ②两道都填满:池仍 30、civic 2 + we 1 + 个人 27 ────────────────────

@pytest.mark.anyio
async def test_both_lanes_full_leaves_the_personal_arm_at_27(db_session, reserves):
    """拍板预算:``civic_reserve=2`` + ``we_reserve=1`` → public 3/30 = 10%,
    个人臂 28 → **27**。

    两侧都要咬:``we_reserve=0`` 时那条实质世界事件**进不去**(它 0.5,池底 0.995,
    这就是被修缺陷本身),开闸后它绕开 importance 排序直接落座。没有前半句,后半句
    可以由一个恒真的池子说出来。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    civic = await _seed_civic(db_session, rid, ["poll_result:p-1", "poll_result:p-2"])
    we = await _seed_world_events(db_session, rid, 5, tier="substantive")

    reserves(2, 0)
    before = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)
    _check_invariants(before, _POOL_CAP)
    assert not ({m.id for m in we} & {m.id for m in before}), \
        "we 道关着时实质世界事件就不该进池,否则这批改动没有缺陷可修"

    reserves(2, 1)
    after = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)
    _check_invariants(after, _POOL_CAP)
    assert len(_civic_of(after)) == 2
    assert len(_we_of(after)) == 1
    assert len(_personal_of(after)) == 27, "个人臂不是 27 —— 两条道吃的坑数不对"
    assert {m.id for m in civic} <= {m.id for m in after}
    # 道内 created_at DESC:要的是「最近那件事」,不是随便一条
    assert _we_of(after)[0].id == we[0].id


@pytest.mark.anyio
async def test_the_world_event_lane_seats_the_newest_substantive_first(
        db_session, reserves):
    """道内定序 ``created_at DESC``。

    按 importance 排在这条道上等于随机:设计反转之后实质事件与琐事**同档**
    (都是直写的 0.5/0.6),道内的 importance 全等。建议开闸值是 1,正因为「1 个坑
    ≈ 记得最近那件事」;2 个坑会让两周前的节庆压住这周的。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    newest, mid, oldest = await _seed_world_events(
        db_session, rid, 3, tier="substantive")

    reserves(0, 2)
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)
    _check_invariants(pool, _POOL_CAP)
    seated = {m.id for m in _we_of(pool)}
    assert seated == {newest.id, mid.id}
    assert oldest.id not in seated, "道内按 created_at DESC:最老那条不该占坑"


# ── ③we 道 0 条:坑退还给个人臂 ───────────────────────────────────────

@pytest.mark.anyio
@pytest.mark.parametrize("we_trivia,we_legacy", [
    (0, 0),     # 一条世界事件都没有
    (40, 0),    # 只有天气(生产 96% 的量)
    (0, 40),    # 只有存量老记忆(没有 tier 键的那 1380 条)
    (20, 20),   # 两种都有
])
async def test_an_empty_world_event_lane_returns_its_seat(
        db_session, reserves, we_trivia, we_legacy):
    """**最容易写错的一处**:``we_reserve=1`` 但一条实质事件都没有时,个人臂是
    **28** 不是 27。

    按 ``cap - civic_reserve - we_reserve`` 硬减的写法在这里会交出一个 29 条的池 ——
    一条静默的能力倒退。个人臂的 limit 必须是 ``cap − (civic实拿 + we实拿)``,
    用**实拿条数**。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    await _seed_civic(db_session, rid, ["poll_result:p-1", "poll_result:p-2"])
    await _seed_world_events(db_session, rid, we_trivia, tier="trivia")
    await _seed_world_events(db_session, rid, we_legacy, tier=None)

    reserves(2, 1)
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)

    _check_invariants(pool, _POOL_CAP)
    assert len(_civic_of(pool)) == 2
    assert _we_of(pool) == [], "琐事档 / 存量行被收进了专用道 —— 判据太宽"
    assert len(_personal_of(pool)) == 28, "we 道没填满的那个坑没有退还给个人臂"


@pytest.mark.anyio
async def test_an_empty_civic_lane_returns_its_seat_to_the_personal_arm_too(
        db_session, reserves):
    """反过来也要成立:还没结过票的世界里 civic 道空,个人臂是 **29**(30 − we 实拿
    1),不是 27。两条道的退还逻辑各自独立。"""
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    await _seed_world_events(db_session, rid, 3, tier="substantive")

    reserves(2, 1)
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)

    _check_invariants(pool, _POOL_CAP)
    assert _civic_of(pool) == []
    assert len(_we_of(pool)) == 1
    assert len(_personal_of(pool)) == 29, "civic 道没填满的坑没有退还给个人臂"


@pytest.mark.anyio
async def test_a_world_with_neither_gets_the_untouched_personal_pool(
        db_session, reserves):
    """两条道都空(开闸后到第一次结票 / 第一件实质事件之间的每一分钟)必须拿到与
    改前**逐字相同**的池,而不是一个空了三个坑的 27 条池。"""
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    await _seed_world_events(db_session, rid, 20, tier="trivia")

    reserves(0, 0)
    before = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)
    reserves(2, 1)
    after = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)

    _check_invariants(after, _POOL_CAP)
    assert len(_personal_of(after)) == _POOL_CAP
    assert [m.id for m in after] == [m.id for m in before]


# ── ④tier='trivia' 不被收 ────────────────────────────────────────────

@pytest.mark.anyio
async def test_weather_never_gets_a_seat_however_wide_the_lane_is(
        db_session, reserves):
    """S3 实测:公共臂 top-41 **全是** importance=0.5 的天气,而且天气真的挤进了
    「现在镇长是谁」的输出。所以这条道认的是 ``tier='substantive'``,不是
    ``source``、也不是「公共通道」。

    ``we_reserve`` 开到比 cap 还大也一条天气都不许收 —— 这是「不给 weather 开任何
    道」那句话的兑现,不是「今天恰好没收到」。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    trivia = await _seed_world_events(db_session, rid, 40, tier="trivia")
    legacy = await _seed_world_events(db_session, rid, 40, tier=None)

    reserves(0, 40)
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)

    _check_invariants(pool, _POOL_CAP)
    assert not ({m.id for m in trivia + legacy} & {m.id for m in pool})
    assert len(_personal_of(pool)) == _POOL_CAP


@pytest.mark.anyio
async def test_only_the_substantive_ones_are_seated_when_both_kinds_are_present(
        db_session, reserves):
    """生产常态:天气 261 : 实质 17 混在一起,而且天气**更新**(每天 5-6 条)。

    道内 ``created_at DESC`` 若少了 tier 判据,拿到的必然是最新那条天气 —— 这条把
    「按时间取最近的」与「按档位筛出实质的」两件事同时钉住。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    # 实质的先写(更老),天气后写(更新)—— 单看时间它排在前面
    substantive = await _seed_world_events(db_session, rid, 2, tier="substantive")
    trivia = await _seed_world_events(db_session, rid, 10, tier="trivia")
    assert trivia[0].created_at > substantive[0].created_at

    reserves(0, 1)
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)

    _check_invariants(pool, _POOL_CAP)
    assert [m.id for m in _we_of(pool)] == [substantive[0].id]
    assert len(_personal_of(pool)) == 29


# ── ⑤两条道不重叠,且都不在个人臂 ─────────────────────────────────────

@pytest.mark.anyio
async def test_the_two_lanes_never_overlap_and_neither_shows_up_twice(
        db_session, reserves):
    """不变量 2。两条道的判据今天是互斥的(``civic_event`` 前缀 vs
    ``source='world_event'`` + tier),但**互斥要有断言钉住** —— 哪天有人把 civic 道
    放宽成「公共通道」,同一条记忆就会被两条道各收一次:池长度仍是 30,去重后只有
    29 条,而「池仍 30」这句话会以假成立的方式过去。

    所以这里直接量两条道的产出交集,而不是只靠池里的零重复 —— 后者在实现里加一行
    去重就能掩盖过去。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    civic = await _seed_civic(db_session, rid, ["poll_result:p-1", "poll_result:p-2"])
    we = await _seed_world_events(db_session, rid, 3, tier="substantive")

    reserves(2, 1)
    svc = MemoryService(db_session)
    civic_lane = await svc._fetch_reserved_civic_candidates(rid, _POOL_CAP)
    we_lane = await svc._fetch_reserved_world_event_candidates(rid, _POOL_CAP)

    assert not ({m.id for m in civic_lane} & {m.id for m in we_lane}), \
        "两条道收到了同一条记忆 —— 有一条道的判据被放宽成了「公共通道」"
    assert {m.id for m in civic_lane} <= {m.id for m in civic}
    assert {m.id for m in we_lane} <= {m.id for m in we}

    pool = await svc._fetch_event_candidates(rid, cap=_POOL_CAP)
    _check_invariants(pool, _POOL_CAP)
    seated = {m.id for m in civic_lane} | {m.id for m in we_lane}
    # 「从个人臂排除」= 池里这些 id 各出现一次,且池里其余全是个人记忆
    assert seated <= {m.id for m in pool}
    assert len([m for m in pool if m.id in seated]) == len(seated)
    assert len(_personal_of(pool)) == _POOL_CAP - len(seated)


@pytest.mark.anyio
async def test_the_merged_pool_is_still_in_importance_order(db_session, reserves):
    """不变量 3。保留位改的是**谁进池**,不是**怎么排** —— 打分公式一字不动。

    设计反转之后两条道的 importance 分了层:镇务结果档 0.99、实质世界事件 0.5,
    而个人臂是 28 条 1.0。所以池尾必须是**那条世界事件**,镇务那两条夹在中间;
    若实现是「专用道拼在最前面」这条会红。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    civic = await _seed_civic(db_session, rid, ["poll_result:p-1", "poll_result:p-2"])
    we = await _seed_world_events(db_session, rid, 1, tier="substantive")

    reserves(2, 1)
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)

    _check_invariants(pool, _POOL_CAP)
    assert pool[-1].id == we[0].id, "0.5 的世界事件没有排在池尾"
    assert [m.id for m in pool[-3:-1]] == [m.id for m in civic], \
        "0.99 的镇务记忆没有排在 1.0 的个人记忆与 0.5 的世界事件之间"


# ── ⑥fail-open 路径:cap < 30 时两条道都不生效 ───────────────────────

@pytest.mark.anyio
@pytest.mark.parametrize("cap", [1, 5, 10, 29])
async def test_fail_open_path_seats_nothing_on_either_lane(db_session, reserves, cap):
    """``cap < POOL_RESERVE_MIN_CAP`` 时**两条道都不生效**。

    ``_search_events`` 把 ``limit``(=10)当 cap 传下去,embedding 拿不到时走这条 ——
    此时池只有 10 条**且没有相关度可言**。在 10 个坑里塞 3 条按 ``created_at DESC``
    盲选的公共记忆 = 30% 的输出被盲选污染。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    civic = await _seed_civic(db_session, rid, ["poll_result:p-1", "poll_result:p-2"])
    we = await _seed_world_events(db_session, rid, 5, tier="substantive")

    reserves(0, 0)
    before = await MemoryService(db_session)._fetch_event_candidates(rid, cap=cap)
    reserves(2, 1)
    after = await MemoryService(db_session)._fetch_event_candidates(rid, cap=cap)

    assert [m.id for m in after] == [m.id for m in before]
    assert not ({m.id for m in civic + we} & {m.id for m in after})


@pytest.mark.anyio
async def test_search_events_the_actual_fail_open_entry_seats_nothing(
        db_session, reserves):
    """从真正的入口(``_search_events``,默认 limit=10)进,而不是手搓 cap=10 ——
    手搓只证明「我以为 fail-open 传的是 10」,走入口才咬得住那条接线本身。"""
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    we = await _seed_world_events(db_session, rid, 5, tier="substantive")

    reserves(2, 1)
    pool = await MemoryService(db_session)._search_events(rid, "")

    assert len(pool) == 10
    assert not ({m.id for m in we} & {m.id for m in pool})


# ── ⑦新居民:总数 < cap 时不越界 ─────────────────────────────────────

@pytest.mark.anyio
@pytest.mark.parametrize("n_personal,refs,n_we,expected", [
    (12, ["poll_result:p-1"], 2, 15),   # 池不满,两条道各拿到坑,余下的全归个人臂
    (12, [], 2, 14),                    # 只有世界事件
    (12, ["poll_result:p-1"], 0, 13),   # 只有镇务
    (0, [], 3, 3),                      # 只有世界事件,连个人史都没有
    (0, [], 0, 0),                      # 彻底的新居民
])
async def test_a_new_resident_pool_is_capped_by_the_total_not_by_the_reserves(
        db_session, reserves, n_personal, refs, n_we, expected):
    """不变量 1 的另一半:``len(pool) == min(cap, 活跃 event 总数)``。

    没被专用道收走的那几条实质世界事件照旧由个人臂捡回来(个人臂不筛 source),
    所以池长度等于总数 —— 专用道在这里必须是 no-op(不改变构成、不越界、不重复),
    而不是硬塞。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, [0.6] * n_personal)
    await _seed_civic(db_session, rid, refs)
    await _seed_world_events(db_session, rid, n_we, tier="substantive")

    reserves(2, 1)
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)

    _check_invariants(pool, expected)


# ── 手滑值 ──────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_a_negative_world_event_reserve_is_treated_as_closed(
        db_session, reserves):
    """手滑写成负数不该炸,也不该反向扩池 —— 按关处理。"""
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    we = await _seed_world_events(db_session, rid, 3, tier="substantive")

    reserves(0, -3)
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)

    _check_invariants(pool, _POOL_CAP)
    assert not ({m.id for m in we} & {m.id for m in pool})


@pytest.mark.anyio
async def test_two_oversized_reserves_cannot_push_the_pool_past_the_cap(
        db_session, reserves):
    """两条道各自比 cap 还大时池仍是 cap 条,个人臂不会变成负数长度。

    1 与 2 是拍板值,但这两个键是运维手上的两个整数 —— 各写成 40 不该产出一个
    80 条的池(那会稀释 ``public/pool < 2/3`` 那条硬门的分母)。civic 道先取,
    world_event 道拿剩下的:两条加起来恰好 cap。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    await _seed_civic(db_session, rid, [f"poll_result:p-{i}" for i in range(20)])
    await _seed_world_events(db_session, rid, 40, tier="substantive")

    reserves(20, 40)
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)

    _check_invariants(pool, _POOL_CAP)
    assert len(_civic_of(pool)) == 20
    assert len(_we_of(pool)) == _POOL_CAP - 20
    assert _personal_of(pool) == []
