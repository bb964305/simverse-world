"""S11 —— 集成终审:世界公共记忆的两笔预算。

「小镇现况」与镇务广播都是**往别人的地盘里塞东西**:前者塞进 system prompt 的
上下文,后者塞进 ``_fetch_event_candidates`` 只有 30 个坑的候选池
(``app/memory/service.py:364``)。两处都没有天然背压 —— 政策白名单再加两条、
公投再开三张、营生清单再长十行,谁都不会报错,只是 NPC 的个人记忆被安静地挤掉
一点。这个文件就是那两笔账。

- **上下文预算**:满配事实(14 人营生 + 6 条政策 + 2 张公投 + 10 个地点 + 自身
  事实)渲染后 < 1200 字符,且在一份生产形状的 system prompt 里占不到四分之一。
- **候选池预算**:一次完整选举闭环后,任一居民 top-30 候选池里 ``source='civic'``
  的占比 < 1/3。M3 的分档(结果 0.9 / 征询 0.6)就是为这条服务的。

两条都走**真链路**取数:事实字典由 ``build_town_facts`` 从库里现算,记忆由真的
结票流程写进去。手搓一份 facts 字典只能算出「我以为它有多长」;手搓几条 Memory
行更是绕过了 K14 的分位归一 —— 那才是决定镇务记忆进不进得了池子的那一步。
"""
import inspect
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select

from app.config import settings
from app.llm.prompt import assemble_system_prompt, format_town_facts
from app.memory.service import EVENT_MEMORY_MAX_CHARS, MemoryService
from app.models.dynamic_location import DynamicLocation
from app.models.issue_stance import IssueStance
from app.models.resident import Resident
from app.models.season import Poll
from app.models.town_treasury import TOWN_KEY, TownTreasury
from app.models.world_event import WorldEvent
from app.services import town_facts_service as tfs, world_event_service
from app.services.civic_memory import MEMORY_SOURCE
from app.services.config_service import ConfigService
from app.services.town_facts_service import DECIDE_FACT_KEYS
from tests.test_civic_memory_broadcast import _HISTORY, _seed_history
from tests.test_civic_memory_integration import _elect, _town

#: 「小镇现况」段的绝对上限(plan S11)。这条是硬的:增幅是相对量,基线一换就漂,
#: 字符数不会。
_FACTS_CHAR_BUDGET = 1200

#: 「小镇现况」段在装配后 prompt 里的占比上限。plan S11 写的是「相对闸关基线的
#: **增幅** < 25%」——那个分母下满配实测 26.0%(2505 → 3156 字符),够不着。与其把
#: 基线做大来迁就它,不如保留阈值数字、把分母换成装配后的总长(等价于增幅 < 1/3,
#: 与下面候选池那条同一个三分之一)。
_FACTS_PROMPT_SHARE_LIMIT = 0.25

#: 候选池里镇务记忆的占比上限。
_CIVIC_POOL_SHARE_LIMIT = 1 / 3

#: ``_retrieve_events`` 真正用的池深 = ``max(max_events * 3, 30)``,默认 10 → 30
#: (app/memory/service.py:364)。相关度只在这 30 条里算,进不来等于没写。
_POOL_CAP = 30

#: 一晚连结几场镇务。生产的 ``seed_civic_agenda`` 一次能开好几张,夜间任务补跑时
#: 还会一起结 —— 单场绿不代表连着来也绿。
_BURST_ROUNDS = 5


# ── 夹具 ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_caches():
    """K11:两层进程内快照都会跨测试串味(事实快照 + ``today`` 依赖的活跃事件)。"""
    tfs._reset_for_tests()
    world_event_service.invalidate_active_cache()
    yield
    tfs._reset_for_tests()
    world_event_service.invalidate_active_cache()


@pytest.fixture
def all_facts_on(monkeypatch):
    """预算算的是上界,所以相关闸门一个都不许关着:事实层总闸、舆论(自身立场)、
    镇财政(镇库那一行)。关一个就是在给自己放水。"""
    monkeypatch.setattr(settings, "civic_facts_enabled", True)
    monkeypatch.setattr(settings, "polis_opinion_enabled", True)
    monkeypatch.setattr(settings, "town_treasury_enabled", True)


@pytest.fixture
def broadcast_on(monkeypatch):
    monkeypatch.setattr(settings, "civic_memory_broadcast_enabled", True)


@pytest.fixture
def realism_on(monkeypatch):
    """生产 ``REALISM_ENABLED=true``:importance 落库前要过 per-resident 分位归一
    (K14)。关着测候选池排名等于测了个假世界 —— 那条路径下 0.9 原样落库,永远排在
    最前面。"""
    monkeypatch.setattr(settings, "realism_enabled", True)


# ── 满配的小镇 ──────────────────────────────────────────────────────────

#: 生产的三位 UGC 居民。满配口径是「14 人 duty」,所以这里连他们也各领一份营生 ——
#: 今天生产的 UGC 其实没有 duty,给上是取上界。
_UGC = (
    ("bai-xing", "白杏", "花房照料"),
    ("gu-wanzhou", "顾晚舟", "夜航记录员"),
    ("chen-yu", "陈屿", "码头搬运"),
)

#: 白名单 6 条政策的生产取值(catalog 默认里 ``tax_rate`` 是 0、``curfew_hours``
#: 是空 —— 那两个渲染出来比真实世界短,拿来算预算是自欺)。
_POLICIES = {
    "tax_rate": 0.05,
    "business_hours": {"open": 8, "close": 20},
    "curfew_hours": [22, 6],
    "npc_default_wage_sc": 5,
    "market_day_discount": 0.9,
    "medical_subsidy_sc": 3,
}

#: 今天生产真开着的两张公投的形状。``options_json`` 里并排躺着计票状态与未生效的
#: 效果:出网净化把它们挡在 prompt 外,预算里也就不该有它们的字数。
_POLLS = (
    ("是否把税率从 5% 提到 6% 以补上镇库的窟窿", [
        {"label": "赞成上调", "npc_votes": 6, "_npc_voters": ["he-qiaoyun", "zhao-qiwen"],
         "effect": {"type": "policy", "key": "tax_rate", "value": 0.06}},
        {"label": "维持 5% 不动", "npc_votes": 5, "_npc_voters": ["a-lan"], "effect": None},
    ]),
    ("要不要请一支商队每月定期来镇上摆摊", [
        {"label": "请,每月一次", "npc_votes": 8, "effect": {"type": "narrative"}},
        {"label": "先试一回再说", "npc_votes": 2, "effect": None},
        {"label": "不请", "npc_votes": 1, "effect": None},
    ]),
)

#: 公投建出来的两座楼(S8)。
_DYNAMIC_PLACES = (("post_office", "邮局"), ("theater", "剧院"))


def _long_issue(topic: str) -> str:
    """顶满 ``IssueStance.issue_key`` 的 ``String(300)``。

    议题键是自由文本,原样折进 prompt 一条就能吃掉四分之一段落 —— 事实层的
    ``_clip`` 挡的就是这个,预算必须在**它挡过之后**算。
    """
    return (topic + "，" + "以及随之而来的一连串细节" * 30)[:300]


_ISSUES = ("镇上到底该不该在东岸花园到码头那一段夜路上装灯",
           "商队每月来一趟会不会把杂货铺的生意抢光",
           "税率上调之后多出来的钱该先修路还是先补诊所")


async def _seed_full_town(db) -> Resident:
    """满配的小镇,返回「正在说话的那位居民」。

    走真实写入形状:preset 的 ``meta_json["duty"]`` 原文、带计票状态的
    ``options_json``、顶满列宽的 ``issue_key``。说话人取 ``prompt_hint`` 最长的
    那位 —— 自身事实那一段的字数由它决定。
    """
    from seed.preset_characters import PRESET_CHARACTERS

    presets = [Resident(slug=c["slug"], name=c["name"], resident_type="npc",
                        district=c["district"], status="idle", tile_x=0, tile_y=0,
                        soul_md=c["soul_md"], persona_md=c["persona_md"],
                        ability_md=c["ability_md"], meta_json=c["meta_json"],
                        mood_json={"label": "平静"})
               for c in PRESET_CHARACTERS]
    db.add_all(presets)
    db.add_all([Resident(slug=slug, name=name, resident_type="resident",
                         district="east_garden", status="idle", tile_x=0, tile_y=0,
                         meta_json={"duty": {"key": slug, "title": title,
                                             "prompt_hint": f"你在镇上{title}。"}})
                for slug, name, title in _UGC])
    db.add(TownTreasury(key=TOWN_KEY, balance_sc=12345))
    db.add_all([DynamicLocation(slug=slug, active=True,
                                data_json={"name": name, "type": "public",
                                           "bounds": [0, 0, 1, 1]})
                for slug, name in _DYNAMIC_PLACES])

    speaker = max(presets, key=lambda r: len(
        ((r.meta_json or {}).get("duty") or {}).get("prompt_hint") or ""))
    for i, topic in enumerate(_ISSUES):
        db.add(IssueStance(issue_key=_long_issue(topic), resident_slug=speaker.slug,
                           stance=0.6 - 0.4 * i, interact_count=3,
                           last_update_at=datetime.now(UTC) - timedelta(minutes=i)))
    for question, options in _POLLS:
        db.add(Poll(question=question, options_json=options, status="open",
                    closes_at=datetime.now(UTC) + timedelta(days=2)))
    # 满配连「今天是集市日」那半句也要算上(M2:唯一判据是活跃世界事件的 payload)。
    db.add(WorldEvent(type="festival", title="集市日", description="摊位摆满了中央广场",
                      payload_json={"market_day": True, "location_id": "central_plaza"},
                      starts_at=datetime.now(UTC) - timedelta(hours=1),
                      ends_at=datetime.now(UTC) + timedelta(hours=1), is_active=True))
    await db.commit()

    cfg = ConfigService(db)
    await cfg.set("current_mayor", "he-qiaoyun", group="civic", updated_by="test")
    for key, value in _POLICIES.items():
        await cfg.set(key, value, group="civic", updated_by="test")
    return speaker


def _memory_at_capacity() -> dict:
    """把记忆段撑到生产调用路径自己的容量上。

    基线里的三个数一个都不手写:``retrieve_context`` 的 ``max_events`` /
    ``max_reflections`` 默认值(chat.py 那条链就是不带参数调它的),以及
    ``EVENT_MEMORY_MAX_CHARS`` —— 事件记忆落库时按它截断,所以单条的**上界**就是它。
    """
    params = inspect.signature(MemoryService.retrieve_context).parameters
    filler = "记" * EVENT_MEMORY_MAX_CHARS

    class _Mem:
        """真 ``Memory`` 行只被读 ``content`` / ``metadata_json`` 两个属性;这里要的
        是长度,不是行为 —— 但也绝不用 ``MagicMock``(K10:属性访问永远有值,长度就
        成了 mock 串的长度)。"""

        def __init__(self, content, metadata_json=None):
            self.content = content
            self.metadata_json = metadata_json

    return {
        "relationship": _Mem(filler, {"tags": ["熟人", "话多", "常来打听镇上的事"]}),
        "reflections": [_Mem(filler) for _ in range(params["max_reflections"].default)],
        "events": [_Mem(filler) for _ in range(params["max_events"].default)],
    }


# ── 上下文预算 ──────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_full_facts_stay_within_the_char_budget(db_session, all_facts_on):
    """满配事实渲染后 < 1200 字符。

    同时点名七类都在:预算达标不能靠「少渲染了几段」—— 那样这条断言会在事实层
    悄悄哑掉的那天变成一句表扬。
    """
    speaker = await _seed_full_town(db_session)
    facts = await tfs.build_town_facts(db_session, speaker)
    text = format_town_facts(facts)

    assert len(facts["duties"]) == 14, "满配口径:11 位 preset + 3 位 UGC 全在岗"
    assert len(facts["places"]) == 10, "8 个静态公共设施 + 邮局 + 剧院"
    for probe in ("现任镇长", "镇上的营生分工", "现行的规矩", "镇库余额",
                  "镇上正在议的事", "今天是", "是集市日", "小镇的公共去处",
                  "你自己的营生", "你对镇上议题的态度"):
        assert probe in text, f"{probe} 这一段没渲染出来,预算是白省的"
    assert "npc_votes" not in text and "_npc_voters" not in text, "计票状态出网了"

    assert len(text) < _FACTS_CHAR_BUDGET, f"小镇现况段 {len(text)} 字符,超预算"


#: 一次「公投轰炸」:50 张全是顶满列宽的自由文本。``POST /polls/propose`` 只要一个
#: Bearer token,``topic`` 与 ``options[].label`` 直进这条链路。
_FLOOD_POLLS = 50
_FLOOD_PLACES = 30


@pytest.mark.anyio
async def test_ugc_flood_cannot_blow_the_char_budget(db_session, all_facts_on):
    """**运行时**的预算保证 —— 上面那条量的是固定合成输入,这条量的是敌手输入。

    满配小镇之上再灌 50 张顶满 ``Poll.question`` 列宽(300 字)、每张 30 个超长选项
    的公投,外加 30 个超长名字的公共地点。这些字符串全部来自玩家:``/polls/propose``
    只要求一个 Bearer token,而它们进每位 NPC 的 system prompt、decide prompt,还经
    ``_clerk_announce`` 广播成全镇 14 人的持久记忆。

    没有条数上限 = 谁都能把整段 prompt 预算买断;没有单条长度上限 = 一张就够。
    「段落 < 1200 字符」必须是读侧的硬保证,不能是「我们喂进去的输入恰好不长」。
    """
    speaker = await _seed_full_town(db_session)
    now = datetime.now(UTC)
    db_session.add_all([
        Poll(question="议" * 300,
             options_json=[{"label": f"{i}号选项" + "项" * 80} for i in range(30)],
             closes_at=now + timedelta(hours=i), status="open")
        for i in range(1, _FLOOD_POLLS + 1)
    ])
    db_session.add_all([
        DynamicLocation(slug=f"hall-{i:03d}", active=True,
                        data_json={"name": f"{i:03d}号" + "楼" * 200,
                                   "type": "public", "bounds": [0, 0, 1, 1]})
        for i in range(_FLOOD_PLACES)
    ])
    await db_session.commit()

    facts = await tfs.build_town_facts(db_session, speaker)
    text = format_town_facts(facts)

    # 各段仍在(截断不等于哑掉 —— 那样这条断言就成了一句表扬)。
    for probe in ("现任镇长", "镇上的营生分工", "现行的规矩", "镇库余额",
                  "镇上正在议的事", "今天是", "小镇的公共去处", "你自己的营生"):
        assert probe in text, f"{probe} 这一段被截没了"
    assert len(text) < _FACTS_CHAR_BUDGET, f"UGC 灌爆后 {len(text)} 字符,超预算"


@pytest.mark.anyio
async def test_town_facts_take_under_a_quarter_of_a_production_shaped_prompt(
        db_session, all_facts_on):
    """满配事实在一份生产形状的 system prompt 里占不到四分之一。

    占比是相对量,分母不钉死就毫无意义(一位刚出生、零记忆的居民,同一段事实能占
    掉三成)。这里的分母逐项对着生产的装配点取:人格三层用 preset 原文、记忆段撑到
    ``retrieve_context`` 的默认容量、天气事件与心情几乎恒在。**人生目标与昨夜的梦
    刻意不给** —— 那两段是条件性的,算进去只会把分母做大、把这条断言变松。
    """
    speaker = await _seed_full_town(db_session)
    facts = await tfs.build_town_facts(db_session, speaker)

    memory = _memory_at_capacity()
    weather = [{"title": "小雨", "description": "细雨落了一整天，街上没什么人。"}]
    base = assemble_system_prompt(speaker, memory, weather)
    full = assemble_system_prompt(speaker, memory, weather, town_facts=facts)

    share = (len(full) - len(base)) / len(full)
    assert share < _FACTS_PROMPT_SHARE_LIMIT, \
        f"基线 {len(base)} → {len(full)} 字符,小镇现况段占 {share:.1%}"


@pytest.mark.anyio
async def test_decide_subset_costs_a_fraction_of_the_full_section(
        db_session, all_facts_on):
    """decide 侧的裁剪子集不该跟玩家对话一样贵。

    玩家对话是人来了才拼一次,decide 是 14 位居民每个 tick 各拼一次 —— 同样一段
    字数在那条链路上要乘以几个数量级。裁剪子集(镇长/今天/在议的事/地点)本来就是
    为 K4 做的,这条顺带把它的成本收益钉住:不到完整段落的一半。
    """
    speaker = await _seed_full_town(db_session)
    facts = await tfs.build_town_facts(db_session, speaker)

    full = format_town_facts(facts)
    trimmed = format_town_facts({k: facts[k] for k in DECIDE_FACT_KEYS})

    assert trimmed, "裁剪子集渲染成空 = decide 那侧白接了一条线"
    assert len(trimmed) * 2 < len(full), \
        f"裁剪子集 {len(trimmed)} 字符 / 完整段 {len(full)} 字符"


# ── 候选池预算 ──────────────────────────────────────────────────────────

async def _autonomous(db) -> list[Resident]:
    """收件人口径(K13/K15):npc + UGC,玩家分身不收。"""
    return list((await db.execute(
        select(Resident).where(Resident.is_autonomous).order_by(Resident.slug)
    )).scalars().all())


async def _pool_share(db, resident_id: str) -> tuple[list, list]:
    """(top-30 候选池, 池里的镇务记忆)。"""
    pool = await MemoryService(db)._fetch_event_candidates(resident_id, cap=_POOL_CAP)
    return pool, [m for m in pool if m.source == MEMORY_SOURCE]


@pytest.mark.anyio
async def test_one_election_leaves_the_candidate_pool_mostly_personal(
        db_session, broadcast_on, realism_on):
    """一次完整选举闭环后,任一居民的 top-30 里镇务记忆占比 < 1/3。

    两侧都要咬:镇务记忆**进得去**(非赢家那条结果记忆必须在池里 —— 否则这条占比
    断言是在为「一条都没写进去」鼓掌),又**占不满**。
    """
    people = await _town(db_session)
    for r in await _autonomous(db_session):
        await _seed_history(db_session, r.id, _HISTORY)
    poll = await _elect(db_session, people)

    for r in await _autonomous(db_session):
        pool, civic = await _pool_share(db_session, r.id)
        assert len(pool) == _POOL_CAP, "池子没满,占比无从谈起"
        assert len(civic) / len(pool) < _CIVIC_POOL_SHARE_LIMIT, \
            f"{r.slug} 的候选池里镇务记忆 {len(civic)}/{len(pool)}"
        if r.id != people["cand_a"].id:      # 赢家的结果那轮被排除,她只有第一人称版本
            assert f"civic:poll_result:{poll.id}" in {
                (m.metadata_json or {}).get("civic_event") for m in civic}, \
                f"{r.slug} 没能把结果记忆挤进池子 —— 写了等于没写"


@pytest.mark.anyio
async def test_a_burst_of_civic_events_cannot_take_over_the_pool(
        db_session, broadcast_on, realism_on):
    """连结五场镇务,占比仍 < 1/3。

    撑住这条的是 M3 的分档:只有结果那一档(0.9)挤得进池子,征询(0.6)归一后落在
    池底。两档并成一档的话,五场就是十条,``10/30`` 正好压死 1/3 这条线。
    """
    people = await _town(db_session)
    for r in await _autonomous(db_session):
        await _seed_history(db_session, r.id, _HISTORY)
    for _ in range(_BURST_ROUNDS):
        await _elect(db_session, people)

    for r in await _autonomous(db_session):
        pool, civic = await _pool_share(db_session, r.id)
        assert len(pool) == _POOL_CAP
        assert len(civic) / len(pool) < _CIVIC_POOL_SHARE_LIMIT, \
            f"{r.slug} 连结 {_BURST_ROUNDS} 场后被镇务记忆占了 {len(civic)}/{len(pool)}"
