"""R2 —— 候选池内的镇务保留位(``REALISM_POOL_CIVIC_RESERVE``)。

``_fetch_event_candidates`` 是 ``ORDER BY importance DESC, created_at DESC
LIMIT cap`` —— 打分公式里那 0.45 的相关度权重是**截断之后**才参与的,于是它
根本不参与入池。2026-08-10 生产只读对拍:

    居民            活跃 event   落库 1.0 条数   池底(第 30 名)   镇务记忆进得去?
    jiang-lin        8355          36            1.0             否(第 31 名)
    zhao-qiwen       8042          44            1.0             否
    chen-tiesheng    6525          27            0.995           否
    chen-yu          1946           7            0.985           是

镇务结果档 raw 0.9 归一后是 0.99 —— **差 0.01 就永远进不去,且病症逐人**。

修法是**池内保留位,不是扩池**::

    pool(cap) = 专用道(最多 N 条 civic:poll_result,道内 created_at DESC)
              ∪ 个人臂(cap − 专用道**实际拿到**的条数)

三条不变量在本文件里各有断言守着:

1. ``len(pool)`` 恒等于 ``min(cap, 该居民的活跃 event 总数)``,与改前逐字相同 ——
   专用道**没填满的坑必须退还给个人臂**。否则还没结过票的世界会拿到一个 28 条
   的池,那是**静默的能力倒退**;
2. 专用道成员**从个人臂排除**,不双份占坑 —— 否则「池仍 30」会以「实际只有 29
   条不同记忆」的方式假成立;
3. 零重复 + 稳定定序:合并后仍按 ``importance DESC, created_at DESC`` 交给打分层,
   打分公式一字不动。

另有两条边界各自成测:``reserve=0`` **逐字节**等于改前(拿 ``git show master:``
装成独立模块对拍返回的 id 序列,不是只对长度),以及 fail-open 路径(``cap<30``)
上专用道必须自动失效。
"""
import functools
import importlib.util
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, UTC
from pathlib import Path

import pytest

from app.config import settings
from app.memory import service as service_mod
from app.memory.service import MemoryService
from app.models.memory import Memory
from app.models.resident import Resident

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 生产池深。``_search_events_scored`` 用 ``max(limit*3, 30)``,``limit`` 默认 10。
_POOL_CAP = 30

#: **生产形状的饱和史**(chen-tiesheng 的形状:池底 0.995)。三段:
#:
#: - 前 28 条落库 1.0 —— 当年各自打赢过窗口的老记忆;
#: - 接着 2 条 0.995 —— 这两条就是候选池的第 29/30 名,也就是保留位开闸后**被挤
#:   掉的恰好那两条**。它们比 1.0 低,所以「掉的是最低的两条」这句话咬得动;
#: - 尾巴 10 条 0.9 —— 只为把总数顶过 30,证明池是被 cap 截断的。
#:
#: 镇务结果档归一后是 0.99 < 0.995 —— 差 0.005 就进不去,和生产同一个病。
_SATURATED = [1.0] * 28 + [0.995] * 2 + [0.9] * 10

#: 镇务结果档在饱和史下的归一落点(见 test_civic_memory_broadcast 的同名常量)。
_CIVIC_NORMALIZED = 0.99


# ── master 对拍:把改前的实现装成独立模块 ────────────────────────────────

@functools.lru_cache(maxsize=1)
def _master_memory_service():
    """``git show master:`` 出改前的 ``service.py``,装成一个独立模块。

    对拍**返回的 id 序列**而不是长度:长度相等的两个池完全可能装着不同的记忆,
    而 ``reserve=0`` 承诺的是「逐字节旧行为」,不是「同样多」。
    """
    src = subprocess.run(
        ["git", "show", "master:backend/app/memory/service.py"],
        cwd=_REPO_ROOT, capture_output=True, text=True, check=True).stdout
    assert "realism_pool_civic_reserve" not in src, (
        "从 master 取到的 service.py 里已经有保留位了 —— 对拍对的是它自己,"
        "这条断言恒绿。master 已经合入本批时应改用合入前的 ref。")
    path = Path(tempfile.gettempdir()) / "_pool_master_service_ref.py"
    path.write_text(src, encoding="utf-8")
    spec = importlib.util.spec_from_file_location("_pool_master_service_ref", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.MemoryService


# ── 播种 ────────────────────────────────────────────────────────────────

async def _resident(db, slug: str = "jiang-lin") -> str:
    r = Resident(slug=slug, name=slug, resident_type="npc")
    db.add(r)
    await db.commit()
    return r.id


async def _seed_personal(db, rid: str, values: list[float]) -> list[Memory]:
    """按 importance 分布铺个人事件史;下标 0 最新,每条相隔一天。

    一天一条是为了让「保留位吃掉的是长期往事」可量:池从 30 收到 28,覆盖的时间
    窗口就少了整整两天,而不是两分钟。
    """
    now = datetime.now(UTC)
    out = []
    for i, imp in enumerate(values):
        mem = Memory(resident_id=rid, type="event", content=f"往事 {i}",
                     importance=imp, source="agent_action",
                     metadata_json={"raw_importance": imp})
        mem.created_at = now - timedelta(days=i)
        db.add(mem)
        out.append(mem)
    await db.commit()
    return out


async def _seed_civic(db, rid: str, refs: list[str], *,
                      importance: float = _CIVIC_NORMALIZED) -> list[Memory]:
    """铺镇务记忆。``refs`` 用完整的 ``civic_event`` 值(``poll_result:x`` /
    ``poll_open:x``),下标 0 最新,每条相隔一分钟 —— 全都比个人史新。"""
    now = datetime.now(UTC)
    out = []
    for i, ref in enumerate(refs):
        mem = Memory(resident_id=rid, type="event", content=f"镇务 {ref}",
                     importance=importance, source="civic",
                     metadata_json={"civic_event": f"civic:{ref}",
                                    "raw_importance": 0.9})
        mem.created_at = now - timedelta(minutes=i + 1)
        db.add(mem)
        out.append(mem)
    await db.commit()
    return out


async def _seed_world_events(db, rid: str, n: int) -> list[Memory]:
    """世界事件记忆(直写档)。S3 已证公共臂 top-41 全是 importance=0.5 的天气,
    所以专用道**只收结果档**,不收整条 ``world_event`` 通道。"""
    now = datetime.now(UTC)
    out = []
    for i in range(n):
        mem = Memory(resident_id=rid, type="event", content=f"今天多云 {i}",
                     importance=0.5, source="world_event",
                     metadata_json={"first_hand": True, "event_id": f"w-{i}"})
        mem.created_at = now - timedelta(minutes=i + 1)
        db.add(mem)
        out.append(mem)
    await db.commit()
    return out


@pytest.fixture
def reserve(monkeypatch):
    def _set(n: int):
        monkeypatch.setattr(settings, "realism_pool_civic_reserve", n)
    return _set


# ── 断言辅助 ────────────────────────────────────────────────────────────

def _key(m: Memory):
    created = m.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return (m.importance or 0.0, created)


def _check_invariants(pool: list[Memory], expected_len: int) -> None:
    """不变量 1(长度)与 3(零重复 + 稳定定序)—— 每条用例都过一遍。

    不变量 2(专用道不双份占坑)恰恰是**靠零重复这条**咬住的:双份占坑的形态就是
    同一个 id 在池里出现两次,或者池长度对得上但去重后只有 29 条。
    """
    assert len(pool) == expected_len, f"池长度 {len(pool)} != {expected_len}"
    ids = [m.id for m in pool]
    assert len(set(ids)) == len(ids), "候选池里有重复记忆 —— 专用道成员被双份占坑了"
    keys = [_key(m) for m in pool]
    assert keys == sorted(keys, reverse=True), \
        "合并后没有按 importance DESC, created_at DESC 定序 —— 打分层拿到的是乱序池"


def _civic_of(pool: list[Memory]) -> list[Memory]:
    return [m for m in pool if m.source == "civic"]


def _personal_of(pool: list[Memory]) -> list[Memory]:
    return [m for m in pool if m.source == "agent_action"]


# ── ①reserve=0:逐字节等于改前 ────────────────────────────────────────

@pytest.mark.anyio
@pytest.mark.parametrize("history,civic_refs,we,cap", [
    # 生产饱和形状 × 有两条结果记忆 —— 开闸后差别最大的那组
    pytest.param(_SATURATED, ["poll_result:p-1", "poll_result:p-2"], 0, 30, id="saturated-2results"),
    # 一条都没结过票的世界(绝大多数时刻的形状)
    pytest.param(_SATURATED, [], 0, 30, id="saturated-no-civic"),
    # 征询档 + 世界事件混在里面
    pytest.param(_SATURATED, ["poll_open:p-1", "poll_result:p-9"], 5, 30, id="mixed-sources"),
    # 新居民:总数不足 cap
    pytest.param([0.6] * 12, ["poll_result:p-1"], 0, 30, id="new-resident"),
    # 空史
    pytest.param([], [], 0, 30, id="empty"),
    # 非 30 的 cap:fail-open 那条(10)、比池深还大的(50)、退化的(1)
    pytest.param(_SATURATED, ["poll_result:p-1", "poll_result:p-2"], 3, 10, id="cap-10"),
    pytest.param(_SATURATED, ["poll_result:p-1", "poll_result:p-2"], 3, 50, id="cap-50"),
    pytest.param(_SATURATED, ["poll_result:p-1"], 0, 1, id="cap-1"),
])
async def test_reserve_zero_returns_the_exact_master_sequence(
        db_session, reserve, history, civic_refs, we, cap):
    """``0 = 逐字节旧行为``。对拍的是 **id 序列全等**,不是长度相等。

    这条是本批的回滚保险:部署先以 ``reserve=0`` 上线(零迁移),开闸是**单独一次
    变更**。若 0 这个值本身已经改变了行为,那次「安全部署」就不安全了。
    """
    reserve(0)
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, history)
    await _seed_civic(db_session, rid, civic_refs)
    await _seed_world_events(db_session, rid, we)

    mine = await MemoryService(db_session)._fetch_event_candidates(rid, cap=cap)
    theirs = await _master_memory_service()(db_session)._fetch_event_candidates(rid, cap=cap)

    assert [m.id for m in mine] == [m.id for m in theirs]


@pytest.mark.anyio
async def test_reserve_zero_is_still_identical_with_a_full_civic_history(
        db_session, reserve):
    """连结五场的世界里 ``reserve=0`` 照样逐字节相同 —— 闸关就是闸关。"""
    reserve(0)
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    await _seed_civic(db_session, rid, [f"poll_result:p-{i}" for i in range(5)])

    for cap in (10, 30, 31):
        mine = await MemoryService(db_session)._fetch_event_candidates(rid, cap=cap)
        theirs = await _master_memory_service()(db_session)._fetch_event_candidates(rid, cap=cap)
        assert [m.id for m in mine] == [m.id for m in theirs], f"cap={cap} 漂了"


# ── ②reserve=2 且有 ≥2 条结果档 ──────────────────────────────────────

@pytest.mark.anyio
async def test_two_results_take_two_seats_and_the_personal_arm_keeps_28(
        db_session, reserve):
    """池仍 30、含那 2 条、个人臂**恰好** 28、零重复。

    这就是 jiang-lin / zhao-qiwen 那 0.01(本文件的 fixture 里是 0.005)——
    ``reserve=0`` 时这两条镇务记忆一条都进不来,开闸后它们绕开 importance 排序
    直接落座。绕开的是**入池**这一步,不是打分:合并后照旧按 importance 定序交
    给打分层。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    civic = await _seed_civic(db_session, rid, ["poll_result:p-1", "poll_result:p-2"])
    civic_ids = {m.id for m in civic}

    reserve(0)
    before = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)
    _check_invariants(before, _POOL_CAP)
    assert not (civic_ids & {m.id for m in before}), \
        "闸关时镇务记忆就不该进池,否则这批改动没有缺陷可修"

    reserve(2)
    after = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)
    _check_invariants(after, _POOL_CAP)
    assert civic_ids <= {m.id for m in after}, "开闸后那两条结果记忆仍不在池里"
    assert len(_civic_of(after)) == 2
    assert len(_personal_of(after)) == 28, "个人臂不是 28 —— 保留位没吃到坑或吃多了"


@pytest.mark.anyio
async def test_the_reserved_lane_seats_the_newest_results_first(db_session, reserve):
    """道内定序 ``created_at DESC`` —— 镇务要的是「刚发生什么」。

    用 importance 排会让上周的选举压住今天的结果:结果档全是同一个 raw 0.9,
    归一落点相近,importance 排序在道内基本等于随机。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    # 下标 0 最新
    newest, mid, oldest = await _seed_civic(
        db_session, rid, ["poll_result:new", "poll_result:mid", "poll_result:old"])

    reserve(2)
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)
    _check_invariants(pool, _POOL_CAP)
    seated = {m.id for m in _civic_of(pool)}
    assert seated == {newest.id, mid.id}
    assert oldest.id not in seated, "道内按 created_at DESC:最老那条不该占坑"


# ── ③只有 1 条结果档:未填满的坑退还 ─────────────────────────────────

@pytest.mark.anyio
async def test_a_single_result_returns_the_unfilled_seat_to_the_personal_arm(
        db_session, reserve):
    """**这是最容易写错的一处**:``reserve=2`` 但只有 1 条结果档时,个人臂是
    **29** 不是 28。

    按 ``cap - reserve`` 硬减的写法在这里会交出一个 29 条的池 —— 一条静默的能力
    倒退,而且现有的 6 处 ``len(pool) == 30`` 断言会当场红。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    (civic,) = await _seed_civic(db_session, rid, ["poll_result:p-1"])

    reserve(2)
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)
    _check_invariants(pool, _POOL_CAP)
    assert civic.id in {m.id for m in pool}
    assert len(_civic_of(pool)) == 1
    assert len(_personal_of(pool)) == 29, "没填满的那个坑没有退还给个人臂"


# ── ④0 条结果档:与改前相同 ────────────────────────────────────────────

@pytest.mark.anyio
async def test_a_world_that_never_voted_gets_the_untouched_personal_pool(
        db_session, reserve):
    """还没结过票的世界(开闸后到第一次结票之间的每一分钟)必须拿到与改前**逐字
    相同**的池,而不是一个空了两个坑的 28 条池。"""
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)

    reserve(0)
    before = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)
    reserve(2)
    after = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)

    _check_invariants(after, _POOL_CAP)
    assert len(_personal_of(after)) == 30
    assert [m.id for m in after] == [m.id for m in before]


# ── ⑤征询档不进专用道 ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_poll_open_notices_are_not_seated(db_session, reserve):
    """只收结果档。征询档(raw 0.6)**不进池** —— 保持 M3 分档:两档并成一档的话
    五场就是 ``10/30``,正好压死 ``public/pool < 1/3`` 那条线。

    NPC 知道「镇上正在议什么」已由**事实层**的 ``town_facts.open_polls`` 提供,
    不必再占一个记忆坑。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    notices = await _seed_civic(
        db_session, rid, ["poll_open:p-1", "poll_open:p-2"],
        importance=settings.civic_memory_notice_importance)

    reserve(2)
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)
    _check_invariants(pool, _POOL_CAP)
    assert not ({m.id for m in notices} & {m.id for m in pool})
    assert len(_personal_of(pool)) == 30, "征询档占了坑 —— 专用道的 LIKE 口径太宽"


@pytest.mark.anyio
async def test_only_the_result_is_seated_when_both_kinds_are_present(
        db_session, reserve):
    """同一个世界里两档并存(生产常态):只有结果档落座,征询档照旧靠 importance
    竞争(它 0.6,竞争不过),个人臂因此是 29。"""
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    (notice,) = await _seed_civic(
        db_session, rid, ["poll_open:p-1"],
        importance=settings.civic_memory_notice_importance)
    (result,) = await _seed_civic(db_session, rid, ["poll_result:p-1"])

    reserve(2)
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)
    _check_invariants(pool, _POOL_CAP)
    assert result.id in {m.id for m in pool}
    assert notice.id not in {m.id for m in pool}
    assert len(_personal_of(pool)) == 29


# ── ⑥world_event 不进专用道 ───────────────────────────────────────────

@pytest.mark.anyio
async def test_world_event_memories_are_not_seated(db_session, reserve):
    """S3 实测:公共臂 top-41 **全是** importance=0.5 的天气,10 个公共坑 100% 被
    天气占,而且天气真的挤进了「现在镇长是谁」的输出。所以专用道认的是
    ``civic:poll_result:`` 这个前缀,不是 ``source`` 也不是「公共通道」。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    we = await _seed_world_events(db_session, rid, 40)

    reserve(2)
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)
    _check_invariants(pool, _POOL_CAP)
    assert not ({m.id for m in we} & {m.id for m in pool})
    assert len(_personal_of(pool)) == 30


# ── ⑦新居民:总数 < cap ────────────────────────────────────────────────

@pytest.mark.anyio
@pytest.mark.parametrize("n_personal,refs,expected", [
    (12, ["poll_result:p-1"], 13),          # 池不满,专用道拿到 1 条
    (12, ["poll_result:p-1", "poll_result:p-2"], 14),
    (12, [], 12),                            # 一条镇务记忆都没有
    (0, ["poll_result:p-1"], 1),             # 只有镇务记忆
    (0, [], 0),                              # 彻底的新居民
])
async def test_a_new_resident_pool_is_capped_by_the_total_not_by_the_reserve(
        db_session, reserve, n_personal, refs, expected):
    """不变量 1 的另一半:``len(pool) == min(cap, 活跃 event 总数)``。

    新居民还有一层:``_normalize_importance`` 在窗口不足 10 条时**直接返回 raw**,
    所以他的镇务记忆落 0.9 而个人记忆落 0.6 —— 本来就进得去。保留位在这里必须
    是 no-op(不改变构成、不越界、不重复),而不是硬塞。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, [0.6] * n_personal)
    await _seed_civic(db_session, rid, refs)

    reserve(2)
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)
    _check_invariants(pool, expected)


# ── ⑧fail-open 路径:cap < 30 时专用道不生效 ─────────────────────────

@pytest.mark.anyio
async def test_the_reserve_floor_matches_the_real_pool_depth():
    """``POOL_RESERVE_MIN_CAP`` 必须等于真实池深。

    这个数一漂,fail-open 的判据就跟着漂:池深若哪天从 30 变了,而这里还写着 30,
    保留位要么在一条不该生效的路径上生效,要么在正路上永远失效。
    """
    import inspect
    limit = inspect.signature(
        MemoryService.retrieve_context).parameters["max_events"].default
    assert service_mod.POOL_RESERVE_MIN_CAP == max(limit * 3, 30)
    assert service_mod.POOL_RESERVE_MIN_CAP == _POOL_CAP


@pytest.mark.anyio
@pytest.mark.parametrize("cap", [1, 5, 10, 29])
async def test_fail_open_path_seats_nothing(db_session, reserve, cap):
    """``cap < 30`` 时专用道**不生效**。

    ``_search_events``(``app/memory/service.py``)把 ``limit``(=10)当 cap 传下去,
    embedding 拿不到时走这条 —— **此时池只有 10 条且没有相关度可言**。在 10 个坑
    里塞 2 条按 ``created_at DESC`` 盲选的公告 = 20% 的输出被盲选污染。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    civic = await _seed_civic(db_session, rid, ["poll_result:p-1", "poll_result:p-2"])

    reserve(0)
    before = await MemoryService(db_session)._fetch_event_candidates(rid, cap=cap)
    reserve(2)
    after = await MemoryService(db_session)._fetch_event_candidates(rid, cap=cap)

    assert [m.id for m in after] == [m.id for m in before]
    assert not ({m.id for m in civic} & {m.id for m in after})


@pytest.mark.anyio
async def test_search_events_the_actual_fail_open_entry_seats_nothing(
        db_session, reserve):
    """从真正的入口(``_search_events``,默认 limit=10)进,而不是手搓 cap=10。

    手搓 cap 只证明了「我以为 fail-open 传的是 10」;走入口才咬得住那条
    ``_fetch_event_candidates(resident_id, limit)`` 的接线本身。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    civic = await _seed_civic(db_session, rid, ["poll_result:p-1", "poll_result:p-2"])

    reserve(2)
    pool = await MemoryService(db_session)._search_events(rid, "")
    assert len(pool) == 10
    assert not ({m.id for m in civic} & {m.id for m in pool})


# ── ⑨掉出去的恰好是最低/最老的那两条 ──────────────────────────────────

@pytest.mark.anyio
async def test_the_two_evicted_are_exactly_the_bottom_of_the_old_pool(
        db_session, reserve):
    """保留位吃的是**池底**,不是随便两条。

    ``_SATURATED`` 下 ``reserve=0`` 的池 = 28 条 1.0 + 2 条 0.995,后两条正是第
    29/30 名。开闸后个人臂必须**逐条等于**旧池去掉尾巴两条 —— 顺序也要一样,
    因为顺序就是打分层看到的东西。

    量出对「长期往事」检索的影响:播种是一天一条,所以个人臂覆盖的时间窗口从
    **29 天**缩到 **27 天**(掉的是最老的两条),即让出两个坑的代价是两天的往事。
    这个数在生产里更小 —— 那里每人每天写 480–545 条 event,池底本来就只有
    13.9–18h 的跨度。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    await _seed_civic(db_session, rid, ["poll_result:p-1", "poll_result:p-2"])

    reserve(0)
    before = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)
    reserve(2)
    after = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)

    personal = _personal_of(after)
    assert [m.id for m in personal] == [m.id for m in before[:28]], \
        "个人臂不是旧池的前 28 条 —— 掉出去的不是池底那两条"

    dropped = [m for m in before if m.id not in {x.id for x in after}]
    assert [m.id for m in dropped] == [m.id for m in before[-2:]]
    assert all(m.importance == pytest.approx(0.995) for m in dropped), \
        "掉的不是 importance 最低的那两条"

    def _age_days(m: Memory) -> int:
        created = m.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return round((datetime.now(UTC) - created).total_seconds() / 86400)

    assert _age_days(before[-1]) == 29
    assert _age_days(personal[-1]) == 27


# ── 定序:合并后仍按 importance DESC, created_at DESC ──────────────────

@pytest.mark.anyio
async def test_seated_memories_are_merged_back_into_importance_order(
        db_session, reserve):
    """不变量 3。保留位改的是**谁进池**,不是**怎么排** —— 打分公式一字不动,
    它拿到的仍是一个按 ``importance DESC, created_at DESC`` 排好的池。

    专用道那两条归一 0.99,低于 28 条 1.0 的个人记忆,所以它们必须排在池**尾**;
    若实现是「专用道拼在最前面」,这条会红。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    civic = await _seed_civic(db_session, rid, ["poll_result:p-1", "poll_result:p-2"])

    reserve(2)
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)
    _check_invariants(pool, _POOL_CAP)
    assert [m.id for m in pool[-2:]] == [m.id for m in civic], \
        "0.99 的镇务记忆没有排在 1.0 的个人记忆之后"


def test_the_lane_predicate_compiles_on_both_dialects():
    """K17:测试跑 sqlite,生产跑 PG —— 专用道的 JSON 路径必须两边都编得出来。

    整个 R2 的行为断言都只在 sqlite 上跑过。JSON 路径是本仓历史上唯一一处
    「sqlite 绿、PG 炸」的形态,所以这条静态断言把两个方言的产物都钉住:PG 走
    ``->>``,sqlite 走 ``JSON_EXTRACT``。
    """
    from sqlalchemy import select as _select
    from sqlalchemy.dialects import postgresql, sqlite

    stmt = _select(Memory.id).where(
        Memory.metadata_json["civic_event"].as_string().like(
            f"{service_mod.CIVIC_RESULT_EVENT_PREFIX}%"))
    def _sql(dialect) -> str:
        return str(stmt.compile(dialect=dialect,
                                compile_kwargs={"literal_binds": True}))

    pg, lite = _sql(postgresql.dialect()), _sql(sqlite.dialect())
    assert "metadata_json ->> 'civic_event'" in pg and "LIKE" in pg, pg
    assert "civic:poll_result:" in pg, pg
    assert "JSON_EXTRACT(memories.metadata_json, '$.\"civic_event\"')" in lite, lite
    assert "LIKE 'civic:poll_result:%'" in lite, lite


@pytest.mark.anyio
async def test_a_negative_reserve_is_treated_as_closed(db_session, reserve):
    """手滑写成负数不该炸,也不该反向扩池 —— 按关处理。"""
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    civic = await _seed_civic(db_session, rid, ["poll_result:p-1"])

    reserve(-3)
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)
    _check_invariants(pool, _POOL_CAP)
    assert civic[0].id not in {m.id for m in pool}


@pytest.mark.anyio
async def test_an_oversized_reserve_cannot_push_the_pool_past_the_cap(
        db_session, reserve):
    """``reserve`` 比 cap 还大时池仍是 cap 条,且个人臂不会变成负数长度。

    2 是拍板值,但这个键是运维手上的一个整数 —— 写成 40 不该产出一个 40 条的池
    (那会稀释 ``public/pool`` 那条硬门的分母)。
    """
    rid = await _resident(db_session)
    await _seed_personal(db_session, rid, _SATURATED)
    await _seed_civic(db_session, rid, [f"poll_result:p-{i}" for i in range(40)])

    reserve(40)
    pool = await MemoryService(db_session)._fetch_event_candidates(rid, cap=_POOL_CAP)
    _check_invariants(pool, _POOL_CAP)
    assert len(_civic_of(pool)) == _POOL_CAP
