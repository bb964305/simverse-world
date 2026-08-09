"""S2 —— 公共事实读取层 ``town_facts_service``。

这一层是「小镇现况」的唯一取数口:镇长 / 在任营生 / 生效政策 / 镇库 / 进行中公
投 / 今天 / 地点。它只负责**读**,不渲染也不注入 —— prompt 拼装是 S3 的事。

三条硬约束在本文件里各有一条断言守着:

- **出网净化**:公投只出 question / options(仅 label) / closes_at。
  ``options_json`` 里那些 ``npc_votes`` / ``_npc_voters`` / ``_proposer_slug`` /
  ``effect`` 是内部计票状态,漏进 prompt 等于把票型和未生效的效果告诉 NPC
  (先例 tests/test_polls_api.py:63 对 API 层的同一条要求)。
- **有界 fail-open**:取数失败时回落上一次快照,但只在
  ``civic_facts_max_stale_seconds`` 之内 —— 宁可不注入,也不注入一个过期的镇长。
- **进程内快照**:同一 worker 里 TTL 内只查一次库(K11 conftest 没有 autouse
  重置,本文件自带 ``_reset_for_tests`` 夹具)。
"""
import json
from datetime import datetime, timedelta, UTC

import pytest
from prometheus_client import REGISTRY

from app import world_clock
from app.config import settings
from app.models.dynamic_location import DynamicLocation
from app.models.resident import Resident
from app.models.season import Poll
from app.models.town_treasury import TOWN_KEY, TownTreasury
from app.models.user import User  # noqa: F401 —— residents.creator_id 的 FK 目标
from app.models.world_event import WorldEvent
from app.services import election_service, town_facts_service as tfs
from app.services import world_event_service
from app.services.config_service import ConfigService


@pytest.fixture(autouse=True)
def _clean_caches():
    """两层进程内快照都会跨测试串味:本模块的事实快照,以及 ``today.is_market_day``
    依赖的 world_event 活跃事件缓存。"""
    tfs._reset_for_tests()
    world_event_service.invalidate_active_cache()
    yield
    tfs._reset_for_tests()
    world_event_service.invalidate_active_cache()


@pytest.fixture
def facts_on(monkeypatch):
    """开事实层总闸(S1 的六个闸门默认全关)。"""
    monkeypatch.setattr(settings, "civic_facts_enabled", True)


def _resident(slug: str, name: str, *, resident_type: str = "npc",
              duty: dict | None = None) -> Resident:
    return Resident(slug=slug, name=name, resident_type=resident_type,
                    meta_json=({"duty": duty} if duty else None))


def _dynamic(slug: str, name: str, *, loc_type: str = "public",
             active: bool = True) -> DynamicLocation:
    """公投/Lab 批出来的世界覆盖层地点(``data_json`` 与 LOCATIONS 条目同构)。"""
    return DynamicLocation(slug=slug, active=active, data_json={
        "name": name, "type": loc_type, "bounds": [0, 0, 1, 1]})


def _sample(labels: dict) -> float:
    return REGISTRY.get_sample_value("civic_facts_failopen_total", labels) or 0.0


async def _elect(db, slug: str) -> None:
    """把某人记成现任镇长(走 system_config 这条 current_mayor 的兜底读法)。"""
    await ConfigService(db).set("current_mayor", slug, group="civic", updated_by="test")


# ── 总闸 ────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_gate_off_returns_empty_dict(db_session):
    """闸关 = 空字典(而不是「字段齐全但值为空」),S3/S4 靠这个 falsy 判空。"""
    assert await tfs.get_town_facts_cached(db_session) == {}


@pytest.mark.anyio
async def test_gate_on_returns_all_seven_sections(db_session, facts_on):
    facts = await tfs.get_town_facts_cached(db_session)
    assert set(facts) == {"mayor", "duties", "policies", "treasury_sc",
                          "open_polls", "today", "places"}


# ── 镇长 ────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_mayor_resolves_slug_and_name(db_session, facts_on):
    db_session.add(_resident("he-qiaoyun", "何巧云"))
    await db_session.commit()
    await _elect(db_session, "he-qiaoyun")

    facts = await tfs.get_town_facts_cached(db_session)
    assert facts["mayor"] == {"slug": "he-qiaoyun", "name": "何巧云"}


@pytest.mark.anyio
async def test_no_mayor_is_none_not_an_exception(db_session, facts_on):
    """镇长之位空缺是常态(任期到期、罢免后),不是异常。"""
    facts = await tfs.get_town_facts_cached(db_session)
    assert facts["mayor"] is None


# ── 营生(duties) ────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_duties_list_autonomous_holders_only(db_session, facts_on):
    """收录口径 = ``is_autonomous``(K15:npc + UGC resident,player 不算),
    且只列真的带 title 的人。"""
    db_session.add_all([
        _resident("zhao-qiwen", "赵启文",
                  duty={"key": "town_clerk", "title": "公告与登记处"}),
        _resident("bai-xing", "白杏", resident_type="resident",
                  duty={"key": "healer", "title": "小镇医士"}),
        _resident("p-chen", "陈铁生分身", resident_type="player",
                  duty={"key": "postman", "title": "邮差"}),
        _resident("a-lan", "阿岚"),  # 无营生
    ])
    await db_session.commit()

    duties = (await tfs.get_town_facts_cached(db_session))["duties"]
    assert duties == [
        {"slug": "bai-xing", "name": "白杏", "title": "小镇医士"},
        {"slug": "zhao-qiwen", "name": "赵启文", "title": "公告与登记处"},
    ], "按 slug 排序保证快照稳定;player 与无营生者不在列"


# ── 政策 ────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_policies_are_whitelist_only(db_session, facts_on):
    facts = await tfs.get_town_facts_cached(db_session)
    assert set(facts["policies"]) == set(tfs.POLICY_WHITELIST)
    for core in ("election_exists", "exile_right", "lab_approval_gate",
                 "approval_routing", "recall_threshold"):
        assert core not in facts["policies"], f"{core} 是宪法核心/路由档,不该进日常对话"


@pytest.mark.anyio
async def test_policies_readable_with_policy_gate_off(db_session, facts_on, monkeypatch):
    """M1:``polis_policy_enabled=False`` 时 ``PolicyService.get`` 回落
    ConfigService,白名单仍然读得到 —— 所以本层用逐键 ``get()`` 而不是闸关直接
    返 ``[]`` 的 ``list_all()``。"""
    monkeypatch.setattr(settings, "polis_policy_enabled", False)
    await ConfigService(db_session).set("tax_rate", 0.07, group="fiscal", updated_by="test")

    policies = (await tfs.get_town_facts_cached(db_session))["policies"]
    assert policies["tax_rate"] == 0.07, "system_config 里的值必须读得到"
    assert policies["business_hours"] == {"open": 8, "close": 20}, \
        "没有 settings 字段的键回落 catalog 原文字面量"
    assert policies["curfew_hours"] == []
    assert policies["medical_subsidy_sc"] == 0


# ── 镇库 ────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_treasury_is_none_when_town_treasury_gate_off(db_session, facts_on):
    """镇财政闸关的世界里根本没有「镇库」这个概念,注入「余额 0」是编事实。
    None 与 0 在 S3 的渲染里语义不同(缺失 = 不渲染,0 = 明说余额为零)。"""
    assert (await tfs.get_town_facts_cached(db_session))["treasury_sc"] is None


@pytest.mark.anyio
async def test_treasury_reads_balance_when_gate_on(db_session, facts_on, monkeypatch):
    monkeypatch.setattr(settings, "town_treasury_enabled", True)
    db_session.add(TownTreasury(key=TOWN_KEY, balance_sc=1234))
    await db_session.commit()

    assert (await tfs.get_town_facts_cached(db_session))["treasury_sc"] == 1234


# ── 进行中公投 ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_open_polls_are_sanitized(db_session, facts_on):
    now = datetime.now(UTC)
    db_session.add(Poll(
        question="是否在东岸花园兴建剧院",
        options_json=[
            {"label": "赞成兴建", "npc_votes": 7,
             "effect": {"type": "dynamic_location", "data": {"slug": "theater"}},
             "_npc_voters": {"a-lan": 0}, "_proposer_slug": "a-lan"},
            {"label": "暂缓,维持现状", "effect": None, "npc_votes": 2},
        ],
        closes_at=now + timedelta(days=2), status="open",
    ))
    db_session.add(Poll(question="上一轮的旧议案", options_json=[{"label": "赞成"}],
                        closes_at=now - timedelta(days=1), status="closed"))
    await db_session.commit()

    open_polls = (await tfs.get_town_facts_cached(db_session))["open_polls"]
    assert [p["question"] for p in open_polls] == ["是否在东岸花园兴建剧院"], \
        "已结票的公投不是「进行中」"
    assert open_polls[0]["options"] == ["赞成兴建", "暂缓,维持现状"]
    assert open_polls[0]["closes_at"].startswith(str(now.year))

    blob = json.dumps(open_polls, ensure_ascii=False)
    for leaked in ("npc_votes", "_npc_voters", "_proposer_slug", "effect", "theater"):
        assert leaked not in blob, f"{leaked} 泄漏进了事实层"


@pytest.mark.anyio
async def test_open_polls_fold_raw_policy_keys_into_spoken_labels(db_session, facts_on):
    """存量公投标题里的**原始政策键**必须在出网前折成中文标签。

    ``policy_service._open_amend_poll`` 造的标题形如 ``将「tax_rate」调整为 0.05``
    —— 生产 2026-08-09 正开着这一张(08-11 12:30 UTC 截止)。写侧改口只治得了新
    数据,**库里已经开着的那张改不了**,而它经 ``DECIDE_FACT_KEYS`` 的 ``open_polls``
    直通 decide prompt,一头撞上 K4 的 ``"tax" not in blob.lower()``。所以这道折叠
    的位置只能是读侧,写侧那道是顺带治本。
    """
    db_session.add(Poll(
        question="将「tax_rate」调整为 0.05",
        options_json=[{"label": "赞成"}, {"label": "反对"}],
        closes_at=datetime.now(UTC) + timedelta(days=2), status="open",
    ))
    await db_session.commit()

    open_polls = (await tfs.get_town_facts_cached(db_session))["open_polls"]
    assert open_polls[0]["question"] == "将「税率」调整为 0.05"
    assert "tax" not in json.dumps(open_polls, ensure_ascii=False).lower()


#: 投票档的政策键(``propose_amend`` 会把它们逐字拼进公投标题)。取五个是因为
#: ``OPEN_POLLS_LIMIT`` 只放五张进来 —— 造六张就会有一张因为被截掉而「不漏」。
_VOTE_TIER_KEYS = ("business_hours", "curfew_hours", "npc_default_wage_sc",
                   "election_interval_days", "housing_development_scale")


@pytest.mark.anyio
async def test_open_polls_fold_every_vote_tier_policy_key(db_session, facts_on):
    """折叠表不能只认 ``tax_rate`` —— 任何投票档的政策键都能被 ``propose_amend``
    拼进公投标题,漏一个就是漏一条英文键进 prompt。"""
    now = datetime.now(UTC)
    db_session.add_all([
        Poll(question=f"将「{key}」调整为 1", options_json=[{"label": "赞成"}],
             closes_at=now + timedelta(minutes=i), status="open")
        for i, key in enumerate(_VOTE_TIER_KEYS)
    ])
    await db_session.commit()

    open_polls = (await tfs.get_town_facts_cached(db_session))["open_polls"]
    assert len(open_polls) == len(_VOTE_TIER_KEYS), "五张都要在,否则这条断言是空转的"
    blob = json.dumps(open_polls, ensure_ascii=False)
    for key in _VOTE_TIER_KEYS:
        assert key not in blob, f"{key} 的英文键名经公投标题漏进了事实层"


# ── 自由文本的量纲上限(UGC 无背压) ──────────────────────────────────────

@pytest.mark.anyio
async def test_open_polls_are_capped_by_count_and_length(db_session, facts_on):
    """公投的**条数**与**单条长度**都要有上限。

    ``POST /polls/propose`` 的 topic / options[].label 是玩家自由文本,而这层读出
    来的东西直接进每位 NPC 的 system prompt 与 decide prompt。条数无上限意味着
    「谁都能开公投」= 谁都能把整个 prompt 预算买断;单条无上限意味着一张公投就够。
    S11 的「段落 < 1200 字符」量的是固定合成输入,不是运行时保证 —— 这条才是。

    取最近截止的 ``OPEN_POLLS_LIMIT`` 张:马上要投的那几张才是「镇上正在议的事」。
    """
    now = datetime.now(UTC)
    db_session.add_all([
        # 顶满 Poll.question 的 String(300) 与 options_json 的「没有列宽」。
        Poll(question="议" * 300,
             options_json=[{"label": f"{i}号选项" + "项" * 80} for i in range(30)],
             closes_at=now + timedelta(days=i), status="open")
        for i in range(1, 51)
    ])
    await db_session.commit()

    open_polls = (await tfs.get_town_facts_cached(db_session))["open_polls"]
    assert len(open_polls) == tfs.OPEN_POLLS_LIMIT
    assert open_polls[0]["closes_at"] < open_polls[-1]["closes_at"], "按最近截止取"
    for p in open_polls:
        assert len(p["question"]) <= tfs.POLL_QUESTION_MAX_CHARS
        assert len(p["options"]) <= tfs.POLL_OPTIONS_LIMIT
        for label in p["options"]:
            assert len(label) <= tfs.POLL_OPTION_MAX_CHARS


@pytest.mark.anyio
async def test_duties_are_capped_by_count_and_length(db_session, facts_on):
    """营生清单跟着居民数长,而 UGC 居民是玩家造的 —— 名字与 title 都不是我们写的。"""
    db_session.add_all([
        # 顶满 Resident.name 的 String(100);title 在 meta_json 里,压根没有列宽。
        _resident(f"ugc-{i:03d}", "名" * 100, resident_type="resident",
                  duty={"key": f"k{i}", "title": "衔" * 200})
        for i in range(40)
    ])
    await db_session.commit()

    duties = (await tfs.get_town_facts_cached(db_session))["duties"]
    assert len(duties) == tfs.DUTIES_LIMIT
    for d in duties:
        assert len(d["name"]) <= tfs.DUTY_NAME_MAX_CHARS
        assert len(d["title"]) <= tfs.DUTY_TITLE_MAX_CHARS


@pytest.mark.anyio
async def test_places_are_capped_by_count_and_length(db_session, facts_on):
    """地点会被公投加(S8 的邮局/剧院就是这么来的),名字来自公投 effect 的 data。"""
    db_session.add_all([_dynamic(f"hall-{i:03d}", f"{i:03d}号" + "楼" * 200)
                        for i in range(40)])
    await db_session.commit()

    places = (await tfs.get_town_facts_cached(db_session))["places"]
    assert len(places) == tfs.PLACES_LIMIT
    for name in places:
        assert len(name) <= tfs.PLACE_MAX_CHARS
    assert "市政厅" in places, "静态公共设施排在前面,不该被动态地点挤掉"


# ── 今天 ────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_today_reads_world_clock(db_session, facts_on):
    today = (await tfs.get_town_facts_cached(db_session))["today"]
    assert today["date"] == world_clock.world_date_key()
    assert today["weekday"] == world_clock.world_weekday()
    assert today["is_market_day"] is False, "没有集市日事件时不能凭 weekday 自己算"


@pytest.mark.anyio
async def test_is_market_day_follows_active_world_event(db_session, facts_on):
    """M2:``market_day_weekday`` 本身是可公投改的政策键,自己算必然与世界漂移。
    唯一判据是活跃世界事件的 payload(与 shop_service.py:125 同一条)。"""
    now = datetime.now(UTC)
    db_session.add(WorldEvent(
        type="festival", title="集市日", description="摊位摆满了中央广场",
        payload_json={"market_day": True, "location_id": "central_plaza"},
        starts_at=now - timedelta(hours=1), ends_at=now + timedelta(hours=1),
        is_active=True,
    ))
    await db_session.commit()

    assert (await tfs.get_town_facts_cached(db_session))["today"]["is_market_day"] is True


# ── 地点 ────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_places_are_public_locations(db_session, facts_on):
    """只列公共设施 —— 私宅和公寓是住址不是「小镇有哪些地方」,而且几十个名字
    会把 S11 的 1200 字预算吃光。"""
    places = (await tfs.get_town_facts_cached(db_session))["places"]
    assert "市政厅" in places and "酒馆" in places
    assert "住宅A" not in places and "星光公寓" not in places


@pytest.mark.anyio
async def test_places_include_active_dynamic_locations(db_session, facts_on):
    """S8:公投建出来的楼也是「小镇有哪些地方」的一部分。

    ``map_data.load_dynamic_locations`` 只在进程启动 / ``sv:world:reload`` 时把
    active 行并进 LOCATIONS,事实层自己再查一次库 —— 新落成的楼不用等下一次
    reload 就能进 prompt,被停用的楼也不会因为还留在内存里而继续挂在镇上。
    """
    db_session.add_all([
        _dynamic("post_office", "邮局"),
        _dynamic("theater", "剧院", active=False),
    ])
    await db_session.commit()

    places = (await tfs.get_town_facts_cached(db_session))["places"]
    assert "邮局" in places
    assert "剧院" not in places, "撤销/停用的楼不该还算作小镇的公共去处"


@pytest.mark.anyio
async def test_places_do_not_double_count_merged_dynamic_locations(
        db_session, facts_on, monkeypatch):
    """已经 reload 过的进程里,同一座楼会被静态遍历和动态查询各数一次 —— 必须
    去重,否则 prompt 里写着「小镇的公共去处:……剧院、剧院」。"""
    from app.agent.map_data import LOCATIONS
    monkeypatch.setitem(LOCATIONS, "theater", {
        "name": "剧院", "type": "public", "bounds": (172, 40, 178, 50)})
    db_session.add(_dynamic("theater", "剧院"))
    await db_session.commit()

    places = (await tfs.get_town_facts_cached(db_session))["places"]
    assert places.count("剧院") == 1


@pytest.mark.anyio
async def test_places_skip_non_public_dynamic_locations(db_session, facts_on):
    """动态地点沿用静态那条口径:只有 public 才是公共去处,Lab 批出来的私宅
    与静态的住宅A 一样不进这份名单。"""
    db_session.add(_dynamic("bai-xing-home", "白杏的小院", loc_type="private"))
    await db_session.commit()

    places = (await tfs.get_town_facts_cached(db_session))["places"]
    assert "白杏的小院" not in places


# ── 进程内快照 ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_snapshot_is_cached_and_invalidatable(db_session, facts_on, monkeypatch):
    calls: list[int] = []
    real = election_service.current_mayor

    async def _counting(db):
        calls.append(1)
        return await real(db)

    monkeypatch.setattr(election_service, "current_mayor", _counting)

    await tfs.get_town_facts_cached(db_session)
    await tfs.get_town_facts_cached(db_session)
    assert len(calls) == 1, "TTL 内第二次调用必须走快照,不再查库"

    tfs.invalidate_town_facts_cache()
    await tfs.get_town_facts_cached(db_session)
    assert len(calls) == 2, "invalidate 之后必须重查"


# ── 有界 fail-open(M7) ──────────────────────────────────────────────────

@pytest.mark.anyio
async def test_fail_open_serves_stale_snapshot_then_gives_up(
        db_session, facts_on, monkeypatch):
    db_session.add(_resident("he-qiaoyun", "何巧云"))
    await db_session.commit()
    await _elect(db_session, "he-qiaoyun")
    assert (await tfs.get_town_facts_cached(db_session))["mayor"]["name"] == "何巧云"

    async def _boom(db):
        raise RuntimeError("db down")

    # TTL 归零强制每次重取(不能用 invalidate:那是「已知快照作废」,语义上就不该
    # 再回落到它),再让镇长这一段恒抛。
    monkeypatch.setattr(settings, "civic_facts_cache_ttl_seconds", 0.0)
    monkeypatch.setattr(election_service, "current_mayor", _boom)

    before = _sample({"reason": "mayor"})
    stale = await tfs.get_town_facts_cached(db_session)
    assert stale["mayor"]["name"] == "何巧云", "max_stale 之内回落上一次快照"
    assert _sample({"reason": "mayor"}) == before + 1, "每次 fail-open 都要记一笔"

    # 把快照时刻往前拨过陈旧上限 → 宁可不注入,也不注入一个过期的镇长。
    tfs._cache["ts"] -= settings.civic_facts_max_stale_seconds + 1
    assert await tfs.get_town_facts_cached(db_session) == {}
    assert _sample({"reason": "mayor"}) == before + 2


@pytest.mark.anyio
async def test_cold_cache_failure_returns_empty(db_session, facts_on, monkeypatch):
    """从没成功取过数时失败 → ``{}``(没有旧快照可回落,不能返回半截事实)。"""
    async def _boom(db):
        raise RuntimeError("db down")

    monkeypatch.setattr(election_service, "current_mayor", _boom)
    assert await tfs.get_town_facts_cached(db_session) == {}
