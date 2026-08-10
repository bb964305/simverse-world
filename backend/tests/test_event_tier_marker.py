"""W1 —— world_event 记忆的显式档位标记(``metadata_json["tier"]``)。

生产实测(2026-08-10):``source='world_event'`` 且带 ``raw_importance`` 的记忆
**0 / 1380**;近 3 天的 world_event 记忆是天气 261 + 节庆 17。也就是说**记忆行里
现在没有任何可靠判据**能把「镇上要修一座剧院」与「今天多云」分开:

- ``source`` 分不开 —— 两者都是 ``world_event``;
- ``importance`` 分不开 —— 直写那条路一律 0.5/0.6;
- ``raw_importance`` 更分不开 —— 它只在 ``REALISM_EVENT_MEMORY_TIERED`` **且**
  ``REALISM_ENABLED`` 都开着时才有,而且是 ``_normalize_importance`` 的副产品,
  不是有意的标记。

于是按 ``source='world_event'`` 开检索专用道 = 94% 抓到天气(S3 已证公共臂 top-41
全是 ``importance=0.5`` 的天气)。本步把 ``_is_trivial_event`` 的结论**落成一个可
查询的标记**,判据一个字不动。

**这个标记与 ``REALISM_EVENT_MEMORY_TIERED`` 无关**,本文件的全部重量都压在这句
话上:

    它描述的是「这条记忆是什么」,不是「走了哪条写入路径」。

所以两条写入路径(琐事直写 / 实质走 ``add_memory``)都要写,而且**同一个事件在两种
闸态下必须落同一个 ``tier`` 值**。否则专用道的召回集就会随一个与它无关的闸门漂移:
闸关的世界里实质事件全被标成琐事,专用道一条都收不到,而这正是生产今天的闸位。

存量 1380 条没有这个键 —— 专用道对它们查不到,这是预期的(它们本来也都是天气和
几条早已过时的节庆),**不回填**(数据变更与开闸不同车)。
"""
import random

import pytest

from app.config import settings
from app.services import world_event_service as wes
from tests.test_event_memory_tier import (
    _DIRECT_CONTENT_CAP, _long_description, _plaza_center, _residents, _rows)


@pytest.fixture
def realism_on(monkeypatch):
    """生产 ``REALISM_ENABLED=true``。实质档走 ``add_memory`` 时归一化只在这个总闸
    下生效,关着测等于把两条路的差异先抹掉。"""
    monkeypatch.setattr(settings, "realism_enabled", True)


@pytest.fixture
def gradient_on(monkeypatch):
    """生产 ``REALISM_INFO_GRADIENT_ENABLED=true``。"""
    monkeypatch.setattr(settings, "realism_info_gradient_enabled", True)


@pytest.fixture
def tiered(monkeypatch):
    def _set(on: bool):
        monkeypatch.setattr(settings, "realism_event_memory_tiered", on)
    return _set


#: 判据的全部分支,一个不落 —— 标记的口径**就是** ``_is_trivial_event``,不是另起
#: 一份。``(event 形状, 期望档位)``。
_TIER_CASES = [
    pytest.param({"type": "weather", "payload_json": {}}, "trivia", id="weather"),
    pytest.param({"type": "festival", "payload_json": {"market_day": True}},
                 "trivia", id="market-day-festival"),
    pytest.param({"type": "festival", "payload_json": {}},
                 "substantive", id="festival"),
    pytest.param({"type": "festival", "payload_json": {"market_day": False}},
                 "substantive", id="festival-market-day-false"),
    pytest.param({"type": "news", "payload_json": {}}, "substantive", id="news"),
    pytest.param({"type": "script", "payload_json": {}}, "substantive", id="script"),
    # 没有 type 的事件(``test_world_events_ops`` 就这么调):不是天气也不是集市日
    # 节庆 → 实质。判据本来就是「琐事白名单」,默认档只能是实质。
    pytest.param({"payload_json": {}}, "substantive", id="no-type"),
]


@pytest.mark.anyio
@pytest.mark.parametrize("gate", [False, True], ids=["gate-off", "gate-on"])
@pytest.mark.parametrize("event,expected", _TIER_CASES)
async def test_every_write_path_marks_the_tier(
        db_session, realism_on, tiered, gate, event, expected):
    """两条写入路径都写标记,且**同一事件在两种闸态下 tier 值相同**。

    这条是本步的全部:``gate`` 参数化跑的是同一批事件形状 —— 闸关时它们全部走直写
    (``db.add(Memory(...))``),闸开时实质那几个改走 ``_write_substantive`` →
    ``add_memory``。两条路的 importance、content 长度、``raw_importance`` 都不同,
    **只有 tier 必须一样**,因为它描述的是记忆本身。

    若实现把标记写在 ``_write_substantive`` 里(而不是两条路共用的 ``_meta()``),
    闸关那半边会当场红 —— 那正是生产今天的闸位。
    """
    tiered(gate)
    await _residents(db_session, 3, (0, 0))

    n = await wes.write_collective_memories(
        db_session, {**event, "id": "tier-1", "description": "镇上发生了一件事"})

    assert n == 3
    rows = await _rows(db_session, "tier-1")
    assert len(rows) == 3
    assert {(m.metadata_json or {}).get("tier") for m in rows} == {expected}


@pytest.mark.anyio
@pytest.mark.parametrize("event,expected", _TIER_CASES)
async def test_the_marker_keeps_the_trivial_predicate_as_the_single_source(
        db_session, realism_on, tiered, event, expected):
    """标记的口径**就是** ``_is_trivial_event``,不是另起一份判据。

    直接拿被测判据对答案是「用答案证明答案」,所以上面那张表写的是期望值;这条反
    过来钉住实现没有分叉:落库的 tier 必须与 ``_is_trivial_event`` 当场算出来的结论
    一致。哪天有人给判据加一档(比如把 ``script`` 也归琐事)而忘了标记这一侧,这条
    会红。
    """
    tiered(False)
    await _residents(db_session, 1, (0, 0))
    shape = {**event, "id": "tier-2", "description": "镇上发生了一件事"}

    await wes.write_collective_memories(db_session, shape)

    mem = (await _rows(db_session, "tier-2"))[0]
    assert mem.metadata_json["tier"] == (
        "trivia" if wes._is_trivial_event(shape) else "substantive")
    assert mem.metadata_json["tier"] == expected


@pytest.mark.anyio
async def test_the_marker_does_not_displace_first_hand_or_event_id(
        db_session, realism_on, gradient_on, tiered):
    """既有元数据一个都不能丢:扩散探针与 gossip 靠 ``event_id`` 追链
    (``tests/test_gossip_secondhand.py``),``first_hand`` 是二手转述的判据。

    metadata 全等而不是「包含」—— 「包含」测不出「顺手多写了一个键」,而
    ``test_event_memory_tier`` 那几条闸关快照断言的正是全等。
    """
    tiered(False)
    cx, cy = _plaza_center()
    await _residents(db_session, 1, (cx, cy))
    content = _long_description("公开课：")

    await wes.write_collective_memories(
        db_session,
        {"id": "keep-1", "type": "festival", "description": content,
         "payload_json": {"location_id": "central_plaza"}},
        rng=random.Random(0))

    mem = (await _rows(db_session, "keep-1"))[0]
    assert mem.metadata_json == {
        "first_hand": True, "event_id": "keep-1", "tier": "substantive"}
    assert mem.content == content[:_DIRECT_CONTENT_CAP]


@pytest.mark.anyio
async def test_an_event_without_an_id_still_carries_the_tier(db_session, realism_on):
    """没有 id 的事件照旧不该凭空多出 ``event_id``,但 tier 一样要有 ——
    专用道认的是 tier,不是 ``event_id``。"""
    await _residents(db_session, 2, (0, 0))

    assert await wes.write_collective_memories(
        db_session, {"title": "丰收节", "description": "田里的作物成熟了"}) == 2

    assert all(m.metadata_json == {"first_hand": True, "tier": "substantive"}
               for m in await _rows(db_session))


@pytest.mark.anyio
async def test_the_substantive_path_keeps_the_tier_next_to_raw_importance(
        db_session, realism_on, tiered):
    """闸开 + 实质 = 走 ``add_memory`` 那条路:它会往 metadata 里塞
    ``raw_importance``(``memory/service.py`` 的归一化留痕)。tier 不能被那次
    ``{**metadata_json, ...}`` 合并冲掉。
    """
    tiered(True)
    await _residents(db_session, 2, (0, 0))

    n = await wes.write_collective_memories(
        db_session, {"id": "sub-tier", "type": "news", "payload_json": {},
                     "description": "镇上要修一座剧院"})

    assert n == 2
    for m in await _rows(db_session, "sub-tier"):
        assert m.metadata_json == {
            "first_hand": True, "event_id": "sub-tier", "tier": "substantive",
            "raw_importance": pytest.approx(settings.realism_event_memory_importance)}
