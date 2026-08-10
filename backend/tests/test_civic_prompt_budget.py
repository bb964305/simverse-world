"""S11 —— 集成终审:世界公共记忆的两笔预算。

「小镇现况」与镇务广播都是**往别人的地盘里塞东西**:前者塞进 system prompt 的
上下文,后者塞进 ``_fetch_event_candidates`` 只有 30 个坑的候选池
(``app/memory/service.py:364``)。两处都没有天然背压 —— 政策白名单再加两条、
公投再开三张、营生清单再长十行,谁都不会报错,只是 NPC 的个人记忆被安静地挤掉
一点。这个文件就是那两笔账。

- **上下文预算**:两个输入类各一条,别合成一个数 ——
  *生产形状*(14 人营生 + 6 条政策 + 2 张公投 + 10 个地点 + 自身事实)渲染后
  < 1200 字符,且在一份生产形状的 system prompt 里占不到四分之一;
  *敌手输入*(每一类都灌到 ``town_facts_service`` 自己那个上限)渲染后 < 1600 ——
  这个数就是那几个量纲常量的算术和,不是另标的一个宽限。
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
from tests.test_civic_memory_broadcast import (
    _HISTORY, _SATURATED_HISTORY, _seed_history)
from tests.test_civic_memory_integration import _elect, _town

#: 「小镇现况」段在**生产形状**下的上限(plan S11)。这条是硬的:增幅是相对量,基线
#: 一换就漂,字符数不会。满配实测 606 字符(截止时间改成相对倒计时前是 616;生产真实
#: 取数比这还短,上游复验在那次改动前报 459)—— 近 2 倍余量。这个数**不该因为下面那
#: 个上界变宽而跟着放松**:两条断言原本共用一个常量,抬上界就等于顺手把「满配也就这
#: 么长」一起放了,所以这里把它们拆开。
_FACTS_CHAR_BUDGET = 1200

#: 「小镇现况」段在**敌手输入**下的绝对上界 = ``town_facts_service`` 那几个量纲常量
#: 的算术和(实测 1579,见该模块顶部的分账),留 21 字余量。
#:
#: 为什么不是继续用 1200:1200 是照生产形状标的,而代码自己允许的上限之和是 1579。
#: 两个数原本合成了一个 —— 于是 flood 测试只能靠「灌到今天的 14 人」而不是灌到
#: ``DUTIES_LIMIT = 20`` 才维持绿,它自称守着的那条不变量其实不成立。
#:
#: 为什么不反过来降 ``DUTIES_LIMIT`` 把上界压回 1200:压不回来。除营生外的七类顶格
#: 就已经 1031 字符,``DUTIES_LIMIT`` 要降到 **5** 才够得着 1200(6 人 → 1201),而生产
#: 今天有 14 位在岗自治居民 —— 那等于让 9 个人从名单上静默消失。降到 14(零增长余量,
#: 再来一位 UGC 就有人被截掉)也只到 1417,依旧不成立。这个上界不是「今天会不会炸」,
#: 是「上限之间怎么分账」:两条都还有 2.6 倍以上的余量。
#:
#: 1579 是**算术**上界,由 ``test_facts_caps_sum_under_the_ceiling`` 逐字段顶格算出来
#: 并咬死(任一常量调宽都当场红)。走真链路的 flood 测试实测只到 1487,差的 92 字分两处:
#:
#: - **地点 57 字**:8 个静态公共设施的名字都短于 ``PLACE_MAX_CHARS`` 且恒排在最前,
#:   库里灌多少动态地点都顶不满那 12 个坑;
#: - **截止倒计时 35 字**(5 张 × 7):顶格那一档是「还有 99 天以上截止」11 字,而
#:   ``_read_open_polls`` 取的是**最近截止**的五张 —— 顶到 ``POLL_CLOSES_IN_MAX_DAYS``
#:   的公投按定义截止得最晚,永远抢不到名额。「条数顶格」与「倒计时顶格」在同一份快照
#:   里结构性互斥,后者由 test_town_facts_service.py::test_absurd_deadline_is_clamped
#:   单独证。灌进来的这几张都在几小时内截止,渲染成「今天截止」4 字。
#:
#: 所以 flood 那条是「读侧真的执行了上限」的证据,算术那条才是「上限之和没有越界」的
#: 警戒线,两条缺一不可。
_FACTS_CHAR_CEILING = 1600

#: 「小镇现况」段在装配后 prompt 里的占比上限。plan S11 写的是「相对闸关基线的
#: **增幅** < 25%」——那个分母下满配当时实测 26.0%,够不着。与其把基线做大来迁就
#: 它,不如保留阈值数字、把分母换成装配后的总长(等价于增幅 < 1/3,与下面候选池那
#: 条同一个三分之一)。今天满配实测 2505 → 3121 字符 = 19.7%;换回原来那个分母是
#: 24.59%,压着线过 —— 一段 hint 剥掉几个动作码、一句截止措辞换个写法就能让它翻面
#: (截止改相对倒计时前正是 24.99%),正是「增幅是相对量、分母一换就漂」的现场例子,
#: 分母的选择照旧作数。
_FACTS_PROMPT_SHARE_LIMIT = 0.25

#: 候选池里镇务记忆的占比上限。
_CIVIC_POOL_SHARE_LIMIT = 1 / 3

#: **合并口径**的占比上限:镇务 + 世界事件这两条公共通道加起来,也不许占过池子的
#: 三分之二 —— 个人记忆保住 1/3。
#:
#: 为什么要单开这一条:两条通道各自守着自己那笔账(镇务 < 1/3),但它们抢的是**同
#: 一个** 30 个坑的池子。world_event 记忆此前恒卡在 0.5-0.6、一条都进不来,所以镇务
#: 那条断言等于在一个只有一个竞争者的池子里量占比;分档把实质世界事件抬进池子之
#: 后,那个前提就不成立了。分开量两条通道会同时漏掉「两条都合规、加起来把个人记忆
#: 挤没了」这种形状。
_PUBLIC_POOL_SHARE_LIMIT = 2 / 3

#: 两条公共通道的 ``memories.source``。civic 那个从实现里取(``MEMORY_SOURCE``),
#: world_event 这个是 ``world_event_service`` 直写与 ``_write_substantive`` 共用的字面量。
_PUBLIC_SOURCES = (MEMORY_SOURCE, "world_event")

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
def event_tier_on(monkeypatch):
    """world_event 记忆分档(``REALISM_EVENT_MEMORY_TIERED``)。

    ``realism_info_gradient_enabled`` 刻意**留在默认的关**:那条路是全知广播,每位
    居民都收 —— 预算算的是上界,梯度开着只会让部分居民收不到、把占比做小。
    """
    monkeypatch.setattr(settings, "realism_event_memory_tiered", True)


@pytest.fixture
def pool_reserve(monkeypatch):
    """候选池内的镇务保留位(``REALISM_POOL_CIVIC_RESERVE``,默认 0 = 关)。

    两笔池账都要在**两个闸位**下各跑一遍:保留位吃的是个人臂的坑,而 civic 记忆
    本来还有一条「靠 importance 竞争入池」的路 —— 两条路会叠加。只跑闸关等于
    在开闸后的生产上没有账。
    """
    def _set(n: int):
        monkeypatch.setattr(settings, "realism_pool_civic_reserve", n)
    return _set


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


#: 灌进来的条数一律 = 代码自己的上限 × 这个倍数。**一个都不写死**:写死「50 张
#: 公投」量的是「今天生产开了几张」,而预算不变量要守的是「代码自己允许到几张」。
#: 上一版把公投灌到 50 张、营生留在生产的 14 人,于是 ``DUTIES_LIMIT = 20`` 那 6
#: 个空位从来没被灌满 —— 测试绿,而它自称守着的那条不变量并不成立。
_FLOOD_FACTOR = 3

#: 自由文本的单条长度也顶到**列宽**,不是顶到 ``_clip`` 的上限:``_clip`` 是被测
#: 对象,拿它当输入尺度等于用答案证明答案。``Poll.question`` / ``Resident.name`` /
#: ``IssueStance.issue_key`` 都是 ``String`` 列,``meta_json`` 里的 duty 压根没有列宽。
_POLL_QUESTION_COL = 300
_RESIDENT_NAME_COL = 100
_ISSUE_KEY_COL = 300
_UNBOUNDED = 500


def _flood_duty_holders(count: int) -> list[Resident]:
    """``count`` 位顶满名字与头衔的自治居民。

    slug 以 ``0`` 打头是**故意**的:``_read_duties`` 按 slug 排序后截
    ``DUTIES_LIMIT`` 条,排在前面才占得到名额 —— 上界要的是「20 个名额全被顶格
    条目占满」,而不是「顶格条目排在生产居民后面、正好被截掉」。
    """
    return [Resident(slug=f"0flood-{i:03d}", name="名" * _RESIDENT_NAME_COL,
                     resident_type="resident", district="east_garden",
                     status="idle", tile_x=0, tile_y=0,
                     meta_json={"duty": {"key": f"flood_{i}",
                                         "title": "头衔" * _UNBOUNDED,
                                         "prompt_hint": "岗" * _UNBOUNDED}})
            for i in range(count)]


async def _seed_flooded_town(db) -> Resident:
    """满配小镇之上,把**每一类**都灌到代码自己的上限之上,返回说话的那位居民。

    营生灌过 ``DUTIES_LIMIT`` 且人人顶满 ``Resident.name`` 列宽与无列宽的 duty
    title、公投灌过 ``OPEN_POLLS_LIMIT`` 且张张顶满 ``Poll.question`` 列宽、地点灌过
    ``PLACES_LIMIT``、自身事实那一段(头衔 / hint / 立场)一并顶格。

    字符上界与装配占比两条共用这一份输入 —— 「同一份顶格输入」才谈得上「修好一条
    没破另一条」;各喂各的,两条断言就各自守着一个自己挑的世界。
    """
    speaker = await _seed_full_town(db)
    now = datetime.now(UTC)
    db.add_all(_flood_duty_holders(tfs.DUTIES_LIMIT * _FLOOD_FACTOR))
    db.add_all([
        Poll(question="议" * _POLL_QUESTION_COL,
             options_json=[{"label": f"{i}号选项" + "项" * _UNBOUNDED}
                           for i in range(tfs.POLL_OPTIONS_LIMIT * _FLOOD_FACTOR)],
             closes_at=now + timedelta(hours=i), status="open")
        for i in range(1, tfs.OPEN_POLLS_LIMIT * _FLOOD_FACTOR + 1)
    ])
    db.add_all([
        DynamicLocation(slug=f"hall-{i:03d}", active=True,
                        data_json={"name": f"{i:03d}号" + "楼" * _UNBOUNDED,
                                   "type": "public", "bounds": [0, 0, 1, 1]})
        for i in range(tfs.PLACES_LIMIT * _FLOOD_FACTOR)
    ])
    # 自身事实也要顶格:它是 per-resident 现算的那一段,与公共快照同一份预算。
    speaker.meta_json = {**(speaker.meta_json or {}),
                         "duty": {"key": "flood_self",
                                  "title": "头衔" * _UNBOUNDED,
                                  "prompt_hint": "岗" * _UNBOUNDED}}
    db.add_all([
        IssueStance(issue_key=("议" * _ISSUE_KEY_COL)[:_ISSUE_KEY_COL - 3] + f"{i:03d}",
                    resident_slug=speaker.slug, stance=0.9, interact_count=3,
                    last_update_at=now + timedelta(minutes=i))
        for i in range(tfs.SELF_STANCE_LIMIT * _FLOOD_FACTOR)
    ])
    # 镇长名字读的是 ``Resident.name`` 同一列,顶格的那位也得能当镇长。
    await db.commit()
    await ConfigService(db).set("current_mayor", "0flood-000",
                                        group="civic", updated_by="test")
    return speaker


def _every_field_at_its_cap() -> dict:
    """一份「每个字段都顶到 ``town_facts_service`` 自己那个常量」的事实字典。

    不查库,也不经 ``_clip`` —— 直接照常量造。走真链路的 flood 测试证明的是「读侧
    真的截了」,这份证明的是另一件事:**这些常量加起来没有越界**。后者靠灌库证不
    出来,因为库里灌不满每一个坑(静态地点名就短于 ``PLACE_MAX_CHARS``)。

    政策那一格用生产取值而不是「顶格」:6 键白名单是定死的,四个财政键经
    ``validate_fiscal_policy_value`` 收成数字,渲染形状由政策目录定死 —— 它没有
    字符上限常量可顶。(``business_hours`` / ``curfew_hours`` 走的不是那条校验,
    真塞进个长字符串会经 ``_policy_text`` 的 ``str(value)`` 兜底原样出网;那是另
    一类口子,得在写侧收,不在这次的账里。)
    """
    return {
        "mayor": {"slug": "x", "name": "名" * tfs.MAYOR_NAME_MAX_CHARS},
        "duties": [{"slug": f"r{i}", "name": "名" * tfs.DUTY_NAME_MAX_CHARS,
                    "title": "衔" * tfs.DUTY_TITLE_MAX_CHARS}
                   for i in range(tfs.DUTIES_LIMIT)],
        "policies": _POLICIES,
        # 镇库是 ``BigInteger``,顶格就是 64 位有符号上界(位数决定字数)。
        "treasury_sc": 2 ** 63 - 1,
        # 倒计时顶格 = 位数顶格那一档:``POLL_CLOSES_IN_MAX_DAYS`` 及以上都渲染
        # 成「还有 99 天以上截止」,那是这一格最长的半句。
        "open_polls": [{"question": "问" * tfs.POLL_QUESTION_MAX_CHARS,
                        "options": ["选" * tfs.POLL_OPTION_MAX_CHARS
                                    for _ in range(tfs.POLL_OPTIONS_LIMIT)],
                        "closes_in_days": tfs.POLL_CLOSES_IN_MAX_DAYS}
                       for _ in range(tfs.OPEN_POLLS_LIMIT)],
        "today": {"date": "2026-08-09", "weekday": 6, "is_market_day": True},
        "places": [chr(ord("一") + i) * tfs.PLACE_MAX_CHARS
                   for i in range(tfs.PLACES_LIMIT)],
        "self": {"duty_title": "衔" * tfs.DUTY_TITLE_MAX_CHARS,
                 "duty_hint": "岗" * tfs.SELF_DUTY_HINT_MAX_CHARS,
                 "stances": [{"issue": chr(ord("一") + i) * tfs._ISSUE_MAX_CHARS,
                              "label": "支持"}
                             for i in range(tfs.SELF_STANCE_LIMIT)]},
    }


def test_facts_caps_sum_under_the_ceiling():
    """量纲上限之和 < ``_FACTS_CHAR_CEILING`` —— 这条才是那句「改这里的任何一个数
    都会被那条测试算总账」的兑现。

    原来的账是这么写的:「营生 ~550 / 公投 ~560 / 地点 ~115,其余是有界的固定形状,
    合计留出余量」。前三项就已经 1225 > 1200,而「其余」还藏着 357 —— 分账**在算术
    上从来就不成立**,只是没有一条断言把它加起来过。加起来是这条测试的全部工作。
    """
    text = format_town_facts(_every_field_at_its_cap())
    assert len(text) < _FACTS_CHAR_CEILING, \
        f"量纲上限之和 {len(text)} 字符,越过了 {_FACTS_CHAR_CEILING} —— " \
        "要么把新增的量纲收回去,要么连同上界与模块顶部的分账一起重标"


@pytest.mark.anyio
async def test_ugc_flood_cannot_blow_the_char_budget(db_session, all_facts_on):
    """**运行时**的字符上界 —— 上面那条量的是固定合成输入,这条量的是敌手输入。

    灌进来的字符串全部来自玩家:``/polls/propose`` 只要求一个 Bearer token,而它们
    进每位 NPC 的 system prompt、decide prompt,还经 ``_clerk_announce`` 广播成全镇
    的持久记忆。

    没有条数上限 = 谁都能把整段 prompt 预算买断;没有单条长度上限 = 一条就够。
    段落字符上限必须是读侧的硬保证,不能是「我们喂进去的输入恰好不长」——**也不能
    是「我们喂进去的条数恰好没顶到代码自己的上限」**。
    """
    speaker = await _seed_flooded_town(db_session)
    facts = await tfs.build_town_facts(db_session, speaker)
    text = format_town_facts(facts)

    # 条数上限逐条咬死:少了任何一条,下面那个字符数就不是上界。
    assert len(facts["duties"]) == tfs.DUTIES_LIMIT
    assert len(facts["open_polls"]) == tfs.OPEN_POLLS_LIMIT
    assert all(len(p["options"]) == tfs.POLL_OPTIONS_LIMIT for p in facts["open_polls"])
    assert len(facts["places"]) == tfs.PLACES_LIMIT
    assert len(facts["self"]["stances"]) == tfs.SELF_STANCE_LIMIT

    # 每一类事实都过了量纲上限 —— 「有上限」是模块自述的不变量,不许有例外字段。
    assert len(facts["mayor"]["name"]) <= tfs.MAYOR_NAME_MAX_CHARS
    assert all(len(d["name"]) <= tfs.DUTY_NAME_MAX_CHARS
               and len(d["title"]) <= tfs.DUTY_TITLE_MAX_CHARS
               for d in facts["duties"])
    assert all(len(p["question"]) <= tfs.POLL_QUESTION_MAX_CHARS
               and all(len(o) <= tfs.POLL_OPTION_MAX_CHARS for o in p["options"])
               for p in facts["open_polls"])
    # 倒计时这一格只能查「有界」,查不到「顶格」——两件事在这份输入里互斥:
    # ``_read_open_polls`` 取的是**最近截止**的五张,而顶到
    # ``POLL_CLOSES_IN_MAX_DAYS`` 的那种公投按定义截止得最晚,永远排在后面被截掉。
    # 顶格那一档由 tests/test_town_facts_service.py::test_absurd_deadline_is_clamped
    # 单独证(它只开那一张,不跟条数上限抢名额)。
    assert all(0 <= p["closes_in_days"] <= tfs.POLL_CLOSES_IN_MAX_DAYS
               for p in facts["open_polls"])
    assert all(len(name) <= tfs.PLACE_MAX_CHARS for name in facts["places"])
    assert len(facts["self"]["duty_title"]) <= tfs.DUTY_TITLE_MAX_CHARS
    assert len(facts["self"]["duty_hint"]) <= tfs.SELF_DUTY_HINT_MAX_CHARS
    assert all(len(s["issue"]) <= tfs._ISSUE_MAX_CHARS
               for s in facts["self"]["stances"])

    # 各段仍在(截断不等于哑掉 —— 那样这条断言就成了一句表扬)。
    for probe in ("现任镇长", "镇上的营生分工", "现行的规矩", "镇库余额",
                  "镇上正在议的事", "今天是", "小镇的公共去处", "你自己的营生",
                  "你对镇上议题的态度"):
        assert probe in text, f"{probe} 这一段被截没了"
    assert len(text) < _FACTS_CHAR_CEILING, f"UGC 灌爆后 {len(text)} 字符,超上界"


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
async def test_flooded_facts_share_stays_under_the_ceiling_derived_bound(
        db_session, all_facts_on):
    """顶格输入下的装配占比 —— 上面那条量的是生产形状,这条量的是同一份敌手输入。

    **这条不用 25%,而且 25% 在这个输入类下压根不成立**(实测 37.4%)。不是放水,是
    两条不变量本来就管着两类输入,合成一个数会同时骗到两边:

    - 25% 是**产品配比**目标,分母是一份生产形状的 prompt(记忆撑满、人格三层),
      量的是「事实这一段有没有喧宾夺主」。满配实测 19.7%。
    - 顶格输入下,要压回 25% 就得让整段塞进 ``base/3 ≈ 835`` 字符 —— 而除营生外的
      七类顶格就已经占了 1031。做不到,除非把公投砍到 2 张、营生砍到 7 人,那是拿
      功能换一个好看的数字。

    所以这条守的是**由字符上界推导出来的**那个占比:段落既然 ≤
    ``_FACTS_CHAR_CEILING``,占比就 ≤ ``ceiling / (base + ceiling)``。它不是新拍的
    阈值,是上一条断言的推论;哪天谁把某个量纲常量调宽,这里和字符上界会一起红。

    顺带钉住这次加固的收益:``self`` 段与镇长名字过 ``_clip`` 之前,同一份输入下这
    个数是 54.8%(``self`` 一段就 1623 字符),现在 37.4%。
    """
    speaker = await _seed_flooded_town(db_session)
    facts = await tfs.build_town_facts(db_session, speaker)

    memory = _memory_at_capacity()
    weather = [{"title": "小雨", "description": "细雨落了一整天，街上没什么人。"}]
    base = assemble_system_prompt(speaker, memory, weather)
    full = assemble_system_prompt(speaker, memory, weather, town_facts=facts)

    share = (len(full) - len(base)) / len(full)
    derived = _FACTS_CHAR_CEILING / (len(base) + _FACTS_CHAR_CEILING)
    assert share <= derived, \
        f"顶格输入下占 {share:.1%},超出字符上界推出的 {derived:.1%}"


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


async def _world_events(db, rounds: int) -> list[str]:
    """连开 ``rounds`` 场**实质档**世界事件(一次性的叙事事件,不是天气/集市日)。

    形状照 ``event_templates`` 真开出来的那类(有 id、有 description、payload 里没
    ``market_day``);走的是与 ``event_cron`` 同一个入口
    (``write_collective_memories``),不手搓 Memory 行 —— 手搓就绕过了 K14 的分位
    归一,而那正是决定这些记忆进不进得了池子的那一步。
    """
    ids = []
    for i in range(rounds):
        event_id = f"we-{i}"
        await world_event_service.write_collective_memories(
            db, {"id": event_id, "type": "news", "payload_json": {},
                 "description": f"镇上要修一座剧院（第 {i} 期工程）"})
        ids.append(event_id)
    return ids


@pytest.mark.anyio
@pytest.mark.parametrize("reserve", [0, 2])
async def test_one_election_leaves_the_candidate_pool_mostly_personal(
        db_session, broadcast_on, realism_on, pool_reserve, reserve):
    """一次完整选举闭环后,任一居民的 top-30 里镇务记忆占比 < 1/3。

    两侧都要咬:镇务记忆**进得去**(非赢家那条结果记忆必须在池里 —— 否则这条占比
    断言是在为「一条都没写进去」鼓掌),又**占不满**。

    ``reserve`` 两个闸位都跑:保留位吃的是个人臂的坑,它与「civic 自己靠 importance
    入池」是**会叠加**的两条路 —— 这笔账必须在开闸口径下重算一遍,而不是假定它
    只在生产的饱和史上才起作用。
    """
    pool_reserve(reserve)
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
@pytest.mark.parametrize("reserve", [0, 2])
async def test_a_burst_of_civic_events_cannot_take_over_the_pool(
        db_session, broadcast_on, realism_on, pool_reserve, reserve):
    """连结五场镇务,占比仍 < 1/3。

    撑住这条的是 M3 的分档:只有结果那一档(0.9)挤得进池子,征询(0.6)归一后落在
    池底。两档并成一档的话,五场就是十条,``10/30`` 正好压死 1/3 这条线。

    保留位不改这条账:专用道也**只收结果档**(``civic:poll_result:%``),征询档
    照旧靠 importance 竞争 —— 它 0.6,竞争不过 0.7 的池底。占比逐格量出来
    (不是只判 < 1/3),这样「征询档哪天被放进专用道」会当场红,而不是把占比从
    5/30 推到 10/30 之后仍然低于阈值、安静地过去。
    """
    pool_reserve(reserve)
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
        # 逐条量:非赢家 5 条结果档;赢家 **0 条** —— 五轮她都是赢家,五轮都被
        # exclude_resident_id 排除,她只有第一人称版本(source 不是 civic)。
        # 征询档一条都不该在池里 —— 五轮共 5 条,进来就是 10/30 压死 1/3。
        assert len(civic) == (0 if r.id == people["cand_a"].id
                              else _BURST_ROUNDS), \
            f"{r.slug} 池里镇务记忆 {len(civic)} 条,与「只有结果档进得来」对不上"
        assert not [m for m in civic
                    if (m.metadata_json or {}).get("civic_event", "")
                    .startswith("civic:poll_open:")], \
            f"{r.slug} 的池里出现了征询档 —— M3 分档或专用道口径被放宽了"


@pytest.mark.anyio
@pytest.mark.parametrize("reserve", [0, 2])
async def test_civic_and_world_events_together_leave_the_pool_mostly_personal(
        db_session, broadcast_on, realism_on, event_tier_on, pool_reserve, reserve):
    """**合并口径**:一轮镇务结票 + 一轮实质世界事件之后,任一居民 top-30 里两条
    公共通道的合计占比 < 2/3 —— 个人记忆保住 1/3。

    分档之前这条测不出东西:world_event 记忆恒卡在 0.5/0.6,连池子都进不去,合并
    占比等于镇务占比。分档把实质事件抬进池子之后,两条通道才真的在抢同一批坑。

    两侧都要咬,和镇务那条一个道理:世界事件**进得去**(否则这条占比断言是在为
    「一条都没写进去」鼓掌),又**占不满**。

    连打 ``_BURST_ROUNDS`` 轮而不是各来一轮:各一轮必然绿(总共两条记忆),量不出
    任何东西。上界口径 —— 梯度关(全员广播)、镇务广播开、分档开,三样一起顶格。

    实测(五轮 × 两条通道):非赢家 public 10/30(镇务结果 5 + 世界事件 5)= 33.3%,
    个人记忆 20/30;赢家 5/30。五条世界事件归一后落 0.8776~0.9111,而池底(第 30
    名)是 0.7 —— 直写那条路的 0.5/0.6 正好卡在池底之下,这就是「写了等于没写」。
    征询档(0.6)照旧进不来,两笔账因此都还有一倍以上余量。

    ``reserve`` 两个闸位都跑。在这份**稀薄史**(``_HISTORY``,池底 0.7)下保留位
    其实是 no-op —— 镇务结果档归一到 0.9571,本来就靠 importance 进得去,专用道
    只是把已经在池里的那两条换个入场方式,构成一条不差。保留位真正**增量**塞进
    池子的形状要到饱和史才量得出来,见下面那条。
    """
    pool_reserve(reserve)
    people = await _town(db_session)
    for r in await _autonomous(db_session):
        await _seed_history(db_session, r.id, _HISTORY)
    for _ in range(_BURST_ROUNDS):
        await _elect(db_session, people)
    await _world_events(db_session, _BURST_ROUNDS)

    for r in await _autonomous(db_session):
        pool, _civic = await _pool_share(db_session, r.id)
        public = [m for m in pool if m.source in _PUBLIC_SOURCES]
        personal = [m for m in pool if m.source not in _PUBLIC_SOURCES]
        assert len(pool) == _POOL_CAP, "池子没满,占比无从谈起"
        assert any(m.source == "world_event" for m in public), \
            f"{r.slug} 的候选池里一条世界事件都没有 —— 分档没生效,这条占比是白过的"
        assert len(public) / len(pool) < _PUBLIC_POOL_SHARE_LIMIT, \
            (f"{r.slug} 的候选池被两条公共通道占了 {len(public)}/{len(pool)}"
             f"(镇务 {len(_civic)} + 世界事件 {len(public) - len(_civic)})")
        assert len(personal) / len(pool) > 1 - _PUBLIC_POOL_SHARE_LIMIT, \
            f"{r.slug} 的个人记忆只剩 {len(personal)}/{len(pool)}"

@pytest.mark.anyio
@pytest.mark.parametrize("reserve", [0, 2])
async def test_the_reserved_lane_costs_exactly_its_own_size_on_a_saturated_pool(
        db_session, broadcast_on, realism_on, event_tier_on, pool_reserve, reserve):
    """**饱和史下的合并口径** —— 保留位对 ``public/pool`` 的贡献上界恰好是
    ``reserve/cap``。

    上面三条池账铺的都是 ``_HISTORY``(池底 0.7)。那是**测试镇独有的稀薄史**:
    镇务结果档归一到 0.9571,本来就靠 importance 进得去,保留位在那份历史上是
    no-op —— 拿它去回答「开闸后两笔预算会不会被碰到」等于没回答。

    这条铺 ``_SATURATED_HISTORY``(池底 **1.0**,生产 jiang-lin 8355 event /
    zhao-qiwen 8042 的形状)。归一的数学上界是 ``(n-1+0.5)/n = 0.995``(窗口
    100),所以在这份历史下**任何**公共记忆都进不了池 —— 于是两个闸位量出的是
    干净的增量:

    - ``reserve=0``:``public = 0/30``。这是被修缺陷本身,连结五场 + 五条实质
      世界事件之后,居民脑子里关于镇上的记忆是**零**;
    - ``reserve=2``:``public = 2/30``(6.7%),且这 2 条**全是** ``poll_result``。
      个人记忆 28/30 —— 离 1/3 那条线还有 2.8 倍余量。

    ⚠️ 这条同时钉住本批**没有**修的那一半:``world_event`` 在饱和史下照旧全丢
    (专用道只收结果档)。S3 已证公共臂 top-41 全是 ``importance=0.5`` 的天气,
    给 world_event 开道等于把 10 个公共坑 100% 让给「今天多云」。要不要给它另开
    一条道是下一个议题,不是这条断言可以顺手放宽的东西。
    """
    pool_reserve(reserve)
    people = await _town(db_session)
    for r in await _autonomous(db_session):
        await _seed_history(db_session, r.id, _SATURATED_HISTORY)
    for _ in range(_BURST_ROUNDS):
        await _elect(db_session, people)
    await _world_events(db_session, _BURST_ROUNDS)

    for r in await _autonomous(db_session):
        pool, civic = await _pool_share(db_session, r.id)
        public = [m for m in pool if m.source in _PUBLIC_SOURCES]
        personal = [m for m in pool if m.source not in _PUBLIC_SOURCES]
        # 赢家五轮都被 exclude_resident_id 排除,她一条第三人称结果记忆都没有。
        expected_civic = 0 if r.id == people["cand_a"].id else min(reserve, _BURST_ROUNDS)
        assert len(pool) == _POOL_CAP, "池子没满,占比无从谈起"
        assert len({m.id for m in pool}) == _POOL_CAP, "池里有重复 —— 双份占坑"
        assert len(civic) == expected_civic, \
            (f"{r.slug} reserve={reserve} 时池里镇务记忆 {len(civic)} 条,"
             f"应为 {expected_civic} —— 保留位的增量不等于它自己的坑数")
        assert all((m.metadata_json or {}).get("civic_event", "")
                   .startswith("civic:poll_result:") for m in civic), \
            f"{r.slug} 的专用道里混进了非结果档"
        assert not [m for m in public if m.source == "world_event"], \
            (f"{r.slug} 的池里出现了 world_event —— 专用道口径被放宽到公共通道了,"
             "而 S3 实测那会让天气占满公共坑")
        assert len(public) == expected_civic
        assert len(public) / len(pool) < _PUBLIC_POOL_SHARE_LIMIT, \
            f"{r.slug} 的候选池被公共通道占了 {len(public)}/{len(pool)}"
        assert len(personal) / len(pool) > 1 - _PUBLIC_POOL_SHARE_LIMIT, \
            f"{r.slug} 的个人记忆只剩 {len(personal)}/{len(pool)}"
        assert len(personal) == _POOL_CAP - expected_civic, \
            "没填满的坑没有退还给个人臂"
