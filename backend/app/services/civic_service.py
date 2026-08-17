"""Civic governance (M3) — proposals → clerk bulletin → NPC+player vote →
execute the winning outcome.

Built entirely on existing pieces: the ``polls``/``votes`` tables (C3), the
town-clerk bulletin duty (M0), and the same three landing channels the Lab
uses (system_config / dynamic_locations overlay + reload / world events).

A civic poll's ``options_json`` entry is::

    {"label": "支持", "effect": {...} | None, "npc_votes": 0}

``effect`` on the winning option is dispatched by :func:`_execute_outcome`.
NPC votes are rule-based (SBTI leaning + relationship to proposer + duty
interest); player votes come through the existing ``votes`` table. Everything
is fail-open and gated by ``settings.civic_polls_enabled``.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, UTC

from sqlalchemy import func, select
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models.resident import Resident
from app.models.season import Poll, Vote

logger = logging.getLogger(__name__)

#: F2 —— 开票那一刻的合格选民数，冻结在 ``options_json[0]`` 上（同
#: ``_npc_voters`` / ``_proposer_slug`` 的 blob-on-opts[0] 约定）。
#:
#: 晋升与撤销都会在投票窗口内移动选民集。若法定人数的分母读结票时的实时
#: :func:`_eligible_voter_count`，一张已经开出去的 poll 的判决门槛会在中途改变。
#: 冻结分母对晋升与撤销**同时免疫**，且改动局限在本模块。
#:
#: 配套的语义决定：**降级者的幽灵票保留**——「投票时具备资格即计票」。降级
#: 只改 ``resident_type``，人还在世界里，那张票是他有资格时投的。
#:
#: E8 补充（2026-07-28）：**物理删除**另论。2026-07-25 的花名册重置删掉了 25
#: 位居民，票却留在 ``options_json`` 里，25 张幽灵票让 13 人小镇里的 2 个真
#: 玩家永远投不赢任何议案。``run_npc_voting`` 现在按「slug 是否还在 residents
#: 表」撤这一类票，并把 ``_npc_voters`` 从扁平 slug 列表升级成
#: ``{slug: option_idx}`` 以便定向回滚（读侧兼容旧 list 格式）。
META_ELIGIBLE_AT_OPEN = "_eligible_at_open"


def _announce_closes_text(days) -> str:
    """把「还剩几个世界日截止」折成一句**自锚定**的话。认不出的形状 → 空串。

    与 prompt 层 ``_poll_closes_text`` 的分工:**换算**(按哪根轴数)两边共用
    ``_closes_in_world_days``,只有**措辞**在这里另起一份 —— 因为两处的时态语域
    不同,而这正是 ``_poll_closes_text`` 自己的 docstring 划下的那条线:

    - ``_poll_closes_text`` 服务「小镇现况」段,那一段是**每次读取时现算**的,所以
      「今天 / 明天 / 还有 N 天」这种以说话时刻为原点的指示性措辞恰好是对的。
    - 这一句进的是 ``broadcast_civic_memory`` 的**永久记忆**:写一次,之后被反复
      retrieve。而 ``format_memory_context`` 把记忆渲染成不带日期戳的裸
      ``- {content}``(app/llm/prompt.py)—— 没有任何东西会告诉 NPC 这句话是什么
      时候说的。指示性措辞在这里会一直自称「现在」:一个月后读到「还有 12 天截止」,
      听着仍像眼下还剩 12 天。

    所以这一份把原点**写进话里**:「自本次公告起 N 天」。它不随读取时刻漂移,因为
    原点不再是「读到这句话的那一刻」,而是公告本身 —— 一句关于过去某个时点的陈述,
    放多久都还是真的。

    负数刻意也折成空串:开票公告不该宣称一张刚开出来的票已经过期。那是调用方传了
    非法窗口的信号(入口闸见 ``routers/polls.py`` 的 ``POLL_DAYS_MIN``),少半句好过
    编一句自相矛盾的话。
    """
    # town_facts_service 在模块层拉 policy_service / election_service,而那两个反过
    # 来 import civic_service(只是也放在函数里)—— 模块层拉就会把这个环坐实。
    from app.services.town_facts_service import POLL_CLOSES_IN_MAX_DAYS

    # bool 是 int 的子类,得单独挡;其余异形(旧形状的 ISO 串、dict)少半句而不是
    # 让整条开票链路崩在一句文案上。
    if not isinstance(days, int) or isinstance(days, bool) or days < 0:
        return ""
    if days == 0:
        return "于公告当天截止"
    if days == 1:
        return "于公告次日截止"
    if days >= POLL_CLOSES_IN_MAX_DAYS:
        # 读侧把值夹在上限,被夹住的真值可能大得多 —— 报确切数字就是编。中文的
        # 「以上」含本数,恰好顶格时这句话也为真。
        return f"自本次公告起 {POLL_CLOSES_IN_MAX_DAYS} 天以上截止"
    return f"自本次公告起 {days} 天截止"


async def propose(
    db,
    topic: str,
    options: list[dict],
    *,
    proposer_slug: str | None = None,
    days: int | None = None,
) -> Poll | None:
    """Open a civic poll and have the town clerk announce it.

    ``options`` is a list of ``{"label": str, "effect": dict | None}``. Returns
    the Poll, or None when civic polls are disabled.
    """
    if not settings.civic_polls_enabled:
        return None
    window = days if days is not None else settings.civic_poll_days
    opts = [
        {"label": o["label"], "effect": o.get("effect"), "npc_votes": 0}
        for o in options
    ]
    if proposer_slug:
        # Same blob-on-opts[0] convention as _npc_voters: the proposer travels
        # with the poll so NPC voting can weigh the relationship (option 0 is
        # the proposer's lead option by convention).
        opts[0]["_proposer_slug"] = proposer_slug
    if opts:
        # F2: freeze the quorum denominator at open time (see
        # META_ELIGIBLE_AT_OPEN). Cheap — one COUNT on the same session.
        opts[0][META_ELIGIBLE_AT_OPEN] = await _eligible_voter_count(db)
    poll = Poll(
        question=topic,
        options_json=opts,
        closes_at=datetime.now(UTC) + timedelta(days=window),
        status="open",
    )
    db.add(poll)
    await db.commit()
    await db.refresh(poll)

    proposer_line = ""
    if proposer_slug:
        proposer = (await db.execute(
            select(Resident).where(Resident.slug == proposer_slug)
        )).scalar_one_or_none()
        if proposer is not None:
            proposer_line = f"本案由 {proposer.name} 提议。"
    # 截止日说成**世界轴**的相对倒计时,不写真实轴的绝对日期。
    #
    # 这条公告经 _clerk_announce → broadcast_civic_memory 落成全体自治居民的一等
    # 记忆,而记忆段是不带日期戳的裸 ``- {content}``
    # (app/llm/prompt.py::format_memory_context)—— 写进去的日期会被原样 retrieve
    # 回 NPC 的「## 记忆」里,永久有效。``closes_at`` 是真实轴上的时刻,与同一份
    # prompt 里走 world_clock 的「今天」相隔约两年(k=4):NPC 照字面读 = 这张正在
    # 议的票两年前就截止了。这与「小镇现况」段修掉的是同一类缺陷,只是落的是永久
    # 记忆,错得更久。
    #
    # 换算(按哪根轴数)复用「小镇现况」那一批的 _closes_in_world_days,不另起第二
    # 份口径;措辞另走 _announce_closes_text —— 那一段是读取时现算的,可以说「还有
    # N 天」,而这一句写进永久记忆后没有日期戳去校正它,原点必须写进话里。理由见
    # _announce_closes_text 的 docstring。
    #
    # 函数内 import 沿用本模块既有姿势(见 _clerk_announce)以避开 import 环。
    from app.services.town_facts_service import _closes_in_world_days
    # 认不出的形状 → 空串,那就整句不提截止(少半句,不编一个日期)。
    closes_text = _announce_closes_text(_closes_in_world_days(poll.closes_at))
    closes_line = f"投票{closes_text}," if closes_text else ""
    await _clerk_announce(
        db,
        f"镇务征询:{topic}",
        f"现就「{topic}」公开征询全镇意见,选项:{'、'.join(o['label'] for o in opts)}。"
        f"{proposer_line}{closes_line}请各位居民踊跃参与。",
        civic_ref=f"poll_open:{poll.id}",
    )
    return poll


# ── M5: governance-driven building agenda ──────────────────────────────

CIVIC_AGENDA: list[dict] = [
    {
        "topic": "在南苑空地兴建一座邮局",
        "proposer_slug": "jiang-lin",
        "options": [
            {"label": "赞成兴建", "effect": {"type": "dynamic_location", "data": {
                "slug": "post_office", "name": "邮局", "type": "public", "role": "logistics",
                "bounds": [44, 100, 48, 106], "center": [46, 103], "entrance": [46, 100],
                "description": "小镇邮局:寄信、收件、时间胶囊的中转站",
                "boosted_actions": ["WORK"],
            }}},
            {"label": "暂缓,维持现状", "effect": None},
        ],
    },
    {
        "topic": "在东岸花园兴建一座剧院",
        "proposer_slug": "zhou-dahe",
        "options": [
            {"label": "赞成兴建", "effect": {"type": "dynamic_location", "data": {
                "slug": "theater", "name": "剧院", "type": "public", "role": "culture",
                "bounds": [172, 40, 178, 50], "center": [175, 45], "entrance": [172, 45],
                "description": "小镇剧院:说书、演展、故事会的舞台",
                "boosted_actions": ["CHAT_RESIDENT", "OBSERVE"],
            }}},
            {"label": "暂缓,维持现状", "effect": None},
        ],
    },
]


async def seed_civic_agenda(db) -> int:
    """Open the standing building proposals (idempotent — a topic already having
    a poll is skipped). These走 the full propose→vote→close→build流程, so the
    town's expansion is itself a civic event. Returns polls opened."""
    if not settings.civic_polls_enabled:
        return 0
    opened = 0
    for item in CIVIC_AGENDA:
        exists = (await db.execute(
            select(Poll).where(Poll.question == item["topic"])
        )).scalars().first()
        if exists:
            continue
        poll = await propose(
            db, item["topic"], item["options"],
            proposer_slug=item.get("proposer_slug"),
        )
        if poll is not None:
            opened += 1
    return opened


# ── NPC voting (rule-based, zero LLM) ──────────────────────────────────

def _voter_map(opts: list[dict]) -> tuple[dict[str, int], bool]:
    """读出 ``_npc_voters``，统一成 ``{slug: option_idx}``。

    返回 ``(voters, is_legacy)``。存量 poll 存的是扁平 ``list[str]``，物理上
    没有票的归属 —— 那些条目映射成 ``-1``，``is_legacy`` 为 True，调用方据此
    知道「知道谁投过，但不知道投了哪一项」。
    """
    raw = (opts[0] or {}).get("_npc_voters") if opts else None
    if isinstance(raw, dict):
        return {str(k): int(v) for k, v in raw.items()}, False
    if isinstance(raw, list):
        return {str(s): -1 for s in raw}, True
    return {}, False


async def run_npc_voting(db) -> int:
    """Each NPC casts one rule-based vote on each open poll it hasn't voted on.

    Votes accumulate in ``options_json[i]['npc_votes']``; a per-poll set of
    voter slugs (stored on the poll options blob under ``_npc_voters``) keeps
    it idempotent across nightly runs. Returns the number of votes cast.
    """
    if not settings.civic_polls_enabled:
        return 0
    polls = (await db.execute(select(Poll).where(Poll.status == "open"))).scalars().all()
    if not polls:
        return 0
    residents = (await db.execute(
        select(Resident).where(Resident.is_civic_voter)
    )).scalars().all()
    if not residents:
        return 0

    # 「还在不在这个世界里」是存在性问题，不是资格问题：上面的 residents 是
    # is_civic_voter 集合（资格），降级者不在里面但人还在。撤票只针对物理
    # 删除的 slug，所以另查一次全表。
    live_slugs = set((await db.execute(select(Resident.slug))).scalars().all())

    from app.services import relation_service
    by_slug = {r.slug: r for r in residents}
    cast = 0
    # 撤票与 list→dict 的格式升级都可能在 cast == 0 时发生（一张所有活人都
    # 投过、只剩幽灵要清的 poll 就是这种情况）。用显式的 changed 追踪是否需要
    # commit —— 只看 cast 会让撤票白做一场。
    changed = False
    for poll in polls:
        opts = list(poll.options_json or [])
        if not opts:
            continue
        voters, is_legacy = _voter_map(opts)
        if is_legacy:
            changed = True   # 落库时会写成 dict，形状本身就变了

        # E8 撤票：投票人已从 residents 表消失（2026-07-25 花名册重置的事故
        # 残留）→ 把票收回。F2 的「投票时具备资格即计票」保护的是**降级者**
        # （行还在、只是 resident_type 变了），那种票保留 —— 见本模块
        # META_ELIGIBLE_AT_OPEN 的注释与 test_civic_frozen_denominator。
        for slug in [s for s in voters if s not in live_slugs]:
            idx = voters.pop(slug)
            changed = True
            if idx < 0 or idx >= len(opts):
                # 旧 list 格式不知道他投了哪一项。减错票会凭空改变某个具体
                # 选项的得票，比留一张来源不明的票更糟 —— 只移出名册，tally
                # 的订正交给按备份数据做的一次性脚本。
                logger.warning(
                    "poll %s: dropping ghost voter %r with unknown ballot "
                    "(legacy list format) — tally left untouched", poll.id, slug)
                continue
            opts[idx]["npc_votes"] = max(0, int(opts[idx].get("npc_votes", 0)) - 1)

        for r in residents:
            if r.slug in voters:
                continue
            idx = await _npc_choice(db, r, poll, opts, relation_service, by_slug)
            opts[idx]["npc_votes"] = int(opts[idx].get("npc_votes", 0)) + 1
            voters[r.slug] = idx
            cast += 1
        opts[0]["_npc_voters"] = dict(sorted(voters.items()))
        poll.options_json = opts
        flag_modified(poll, "options_json")
    if cast or changed:
        await db.commit()
    return cast


# ── option scoring internals (fix: structural option-0 bias) ───────────
#
# ops-audit-2026-07-25B §A measured 14/14 NPC votes on option 0 across all
# three production polls (normalised entropy 0). The SBTI backfill closed the
# *data* gap, but a static replay still predicted 92.9%–100% — the monopoly is
# structural in the scorer:
#   (1) A2="M" scored nothing at all, and 10 of 14 production NPCs are M;
#   (2) the tie-break `(scores[i], -i)` is pure index order, so every all-zero
#       row lands on index 0;
#   (3) an election poll gives *every* option an effect, which silences the
#       `H and not eff` branch and makes `L and eff` uniform → tie → index 0.
# The three fixes below are numbered to match.

# (2) Personal taste: a deterministic per-(resident, poll, option) draw that
# replaces the index tie-break. Strictly smaller than every trait signal
# (smallest trait step is 0.30), so it can only ever decide a genuine tie —
# but large enough to split residents whose SBTI vectors are identical, which
# is exactly the production situation (10 NPCs all A2=M).
_TASTE_MAG = 0.25

# (3) Topic tags read off the *content* of an effect, so options that all carry
# an effect can still be told apart.
_TOPIC_KEYWORDS: dict[str, tuple[str, ...]] = {
    "social": ("chat_resident", "tavern", "cafe", "剧院", "酒馆", "咖啡",
               "聚会", "社交", "故事", "演"),
    "economy": ("shop", "market", "price", "wage", "tax", "treasury", "经济",
                "商", "价格", "工资", "税", "财政", "集市"),
    "order": ("policy", "system_config", "规则", "制度", "条例", "秩序", "章程"),
    "build": ("dynamic_location", "兴建", "工程", "修建", "建一座"),
    "office": ("mayor", "election", "镇长", "选举", "office"),
    "culture": ("observe", "library", "culture", "书", "讲", "学堂", "展",
                "文化", "艺"),
}

# Effect types a 务实中间派 considers walk-back-able (a knob you can turn
# again) as opposed to irreversible commitments (a building, an office).
_REVERSIBLE_TYPES = frozenset({"system_config", "policy", "narrative"})

# Effect types whose payload names a *person* — the option's identity is the
# candidate, not the topic, so score it by the voter's tie to that resident.
_PERSON_TYPES = frozenset({"mayor", "office", "duty"})

# (dimension, level, tag, delta) — only explicit H/L levels emit a signal, so a
# resident with a partial profile keeps today's behaviour.
_TRAIT_AFFINITY: tuple[tuple[str, str, str, float], ...] = (
    # A1 世界观乐观度: optimists back change, skeptics back the status quo.
    ("A1", "H", "change", 0.30),
    ("A1", "L", "status_quo", 0.30),
    # So1 社交能量: extroverts want more places and occasions to meet.
    ("So1", "H", "social", 0.30),
    ("So1", "L", "social", -0.30),
    # Ac1 成就动机: the ambitious care who holds office, and about money.
    ("Ac1", "H", "office", 0.30),
    ("Ac1", "H", "economy", 0.30),
    ("Ac1", "L", "office", -0.30),
    # E1 表达欲: expressive residents favour culture and stages.
    ("E1", "H", "culture", 0.30),
)


def _stable_unit(*parts: object) -> float:
    """Deterministic float in ``[0, 1)`` from a stable digest of *parts*.

    Explicitly NOT :func:`hash` (PYTHONHASHSEED-salted → different per process)
    and NOT :mod:`random` (unreproducible). Same inputs ⇒ same value on every
    machine, every run — which is what makes the tie-break auditable.
    """
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return int.from_bytes(hashlib.blake2b(raw, digest_size=8).digest(), "big") / 2 ** 64


def _option_key(o: dict, i: int) -> str:
    """Stable identity of an option: its position *and* its content, so the same
    recurring question (elections repeat verbatim) reshuffles when the actual
    options change."""
    eff = o.get("effect") if isinstance(o.get("effect"), dict) else None
    tail = ""
    if eff:
        tail = str(eff.get("slug") or eff.get("key") or eff.get("type") or "")
    return f"{i}:{o.get('label', '')}:{tail}"


def _effect_tags(eff) -> set[str]:
    """Topic tags for one option's effect (``None`` → the status-quo option)."""
    if not eff:
        return {"status_quo"}
    tags = {"change"}
    etype = eff.get("type") if isinstance(eff, dict) else None
    if etype:
        tags.add(f"type:{etype}")
        if etype in _REVERSIBLE_TYPES:
            tags.add("reversible")
    blob = str(eff).lower()
    for topic, keywords in _TOPIC_KEYWORDS.items():
        if any(k in blob for k in keywords):
            tags.add(topic)
    return tags


async def _npc_choice(db, resident, poll, opts, relation_service, by_slug=None) -> int:
    """Score each option for this resident and return the best index.

    Heuristics (all rule-based, zero LLM, fully deterministic):
      - the SBTI A2 axis (规则与灵活度): H=守序 backs the status quo, L=叛逆
        backs change, and M=务实 — 71% of the production cast — prefers changes
        it can walk back rather than scoring nothing at all;
      - other dimensions (A1/So1/Ac1/E1) are matched against topic tags read off
        the effect's content, so a poll whose options *all* carry an effect can
        still differentiate;
      - person-shaped effects (mayor/office) are scored by the voter's tie to
        that candidate — you back yourself, and you back your friends;
      - a positive tie to the proposer nudges toward the proposer's lead option
        (index 0 by convention); the proposer backs their own proposal;
      - a shopkeeper/economy duty leans toward options whose effect touches the
        shop or economy;
      - ties are broken by a deterministic per-resident taste hash, never by
        index order.

    ``settings.civic_npc_choice_legacy`` (env ``CIVIC_NPC_CHOICE_LEGACY``) falls
    back to the pre-fix scorer byte-for-byte — kill switch only, the fix is on
    by default because the default-off variant means production stays broken.
    """
    if settings.civic_npc_choice_legacy:
        return await _npc_choice_legacy(db, resident, poll, opts, relation_service, by_slug)

    sbti = (resident.meta_json or {}).get("sbti", {})
    dims = sbti.get("dimensions", {})
    a2 = dims.get("A2", "M")  # 规则与灵活度: H=守序
    # S10:按人读营生只走 duty_service(手写 meta_json 链在 "duty": None 时会
    # 抛 AttributeError,且绕开未来所有读法变更)。取值与旧链逐字节相同。
    from app.services.duty_service import duty_key as _duty_key
    duty = _duty_key(resident)
    # Recurring polls (elections repeat verbatim) key off the question, not the
    # per-run poll id, so a replay of the same fixture is bit-identical.
    poll_key = getattr(poll, "question", None) or getattr(poll, "id", "")

    scores = [0.0] * len(opts)
    for i, o in enumerate(opts):
        eff = o.get("effect")
        tags = _effect_tags(eff)

        # (1) A2 — every level now emits a signal, including M.
        if a2 == "H":
            if "status_quo" in tags:
                scores[i] += 1.0     # 守序: the status quo needs no defence
            if "order" in tags:
                scores[i] += 0.4     # …and a rule-level change is at least legible
        elif a2 == "L":
            if "change" in tags:
                scores[i] += 0.5     # 叛逆者 lean toward change
        else:                        # "M" — 务实中间派
            # Deliberately *not* a blanket pro- or anti-change lean: 71% of the
            # production cast is M, so any uniform tilt just swaps an option-0
            # monopoly for an option-1 one. The pragmatist's actual signal is
            # reversibility — back what you can walk back. When no option is
            # reversible (a building, an election), the M block is decided by
            # the other dimensions and by personal taste, i.e. it splits.
            if "reversible" in tags:
                scores[i] += 0.35

        # (3) topic fit from the remaining dimensions
        for code, level, tag, delta in _TRAIT_AFFINITY:
            if dims.get(code) == level and tag in tags:
                scores[i] += delta

        # duty interest
        if eff and duty in ("shop_keeper", "tavern_hub", "cafe_host"):
            blob = str(eff)
            if any(k in blob for k in ("shop", "market", "经济", "price")):
                scores[i] += 0.8

        # (2) deterministic personal taste — replaces the index tie-break
        scores[i] += _TASTE_MAG * _stable_unit(resident.slug, poll_key, _option_key(o, i))

    # (3) person-shaped effects: score the candidate, not the topic.
    for i, o in enumerate(opts):
        eff = o.get("effect")
        if not isinstance(eff, dict) or eff.get("type") not in _PERSON_TYPES:
            continue
        target = eff.get("slug")
        if not target:
            continue
        if target == resident.slug:
            scores[i] += 2.0         # you stand for yourself
            continue
        other = (by_slug or {}).get(target)
        if other is None:
            continue
        # F1 第 2 项:声誉只在这里影响选票。候选集选取已与声誉解耦
        # (election_service.open_election),被动选举权不因名声受损而剥夺。
        # 闸门关时 vote_trust_delta 返回 0.0 → 逐字节等价于改动前。
        from app.services.reputation_service import vote_trust_delta
        scores[i] += vote_trust_delta(other.meta_json)
        try:
            pair = await relation_service.get_pair(db, resident.id, other.id)
            if pair is not None and pair.affinity:
                scores[i] += 1.5 * pair.affinity
        except Exception:
            logger.debug("candidate relation lookup failed", exc_info=True)

    # Relationship-to-proposer nudge toward option 0 (the proposer's lead).
    proposer_slug = opts[0].get("_proposer_slug") if opts else None
    if proposer_slug:
        if resident.slug == proposer_slug:
            scores[0] += 2.0  # you back your own proposal
        else:
            proposer = (by_slug or {}).get(proposer_slug)
            if proposer is not None:
                try:
                    pair = await relation_service.get_pair(db, resident.id, proposer.id)
                    if pair is not None and pair.affinity > 0:
                        # a close friend's ask outweighs mild conservatism
                        scores[0] += 1.5 * pair.affinity
                except Exception:
                    logger.debug("proposer relation lookup failed", exc_info=True)
    # Taste is already baked into `scores`; `-i` only settles an exact float
    # collision, which the digest makes vanishingly unlikely.
    best = max(range(len(opts)), key=lambda i: (scores[i], -i))
    return best


async def _npc_choice_legacy(db, resident, poll, opts, relation_service, by_slug=None) -> int:
    """Pre-2026-07-25 scorer, kept verbatim behind ``CIVIC_NPC_CHOICE_LEGACY``.

    Known broken: A2="M" is a zero signal and the tie-break is index order, so
    an all-tie poll sends 100% of the NPC votes to option 0. Kill switch only.
    """
    sbti = (resident.meta_json or {}).get("sbti", {})
    dims = sbti.get("dimensions", {})
    a2 = dims.get("A2", "M")  # 规则与灵活度: H=守序
    # S10:同 _npc_choice——读法换成官方访问器,取值不变,legacy 打分逻辑未动。
    from app.services.duty_service import duty_key as _duty_key
    duty = _duty_key(resident)

    scores = [0.0] * len(opts)
    for i, o in enumerate(opts):
        eff = o.get("effect")
        # conservative → prefer the no-effect / status-quo option
        if a2 == "H" and not eff:
            scores[i] += 1.0
        if a2 == "L" and eff:
            scores[i] += 0.5  # 叛逆者 lean toward change
        # duty interest
        if eff and duty in ("shop_keeper", "tavern_hub", "cafe_host"):
            blob = str(eff)
            if any(k in blob for k in ("shop", "market", "经济", "price")):
                scores[i] += 0.8

    # Relationship-to-proposer nudge toward option 0 (the proposer's lead).
    proposer_slug = opts[0].get("_proposer_slug") if opts else None
    if proposer_slug:
        if resident.slug == proposer_slug:
            scores[0] += 2.0  # you back your own proposal
        else:
            proposer = (by_slug or {}).get(proposer_slug)
            if proposer is not None:
                try:
                    pair = await relation_service.get_pair(db, resident.id, proposer.id)
                    if pair is not None and pair.affinity > 0:
                        # a close friend's ask outweighs mild conservatism
                        scores[0] += 1.5 * pair.affinity
                except Exception:
                    logger.debug("proposer relation lookup failed", exc_info=True)
    # deterministic tie-break: index order
    best = max(range(len(opts)), key=lambda i: (scores[i], -i))
    return best


# ── closing + execution ────────────────────────────────────────────────

async def close_due_polls(db, now: datetime | None = None) -> int:
    """Close every open poll past its ``closes_at``, tally NPC+player votes,
    execute the winner's effect, and announce the result. Returns count closed."""
    if not settings.civic_polls_enabled:
        return 0
    now = now or datetime.now(UTC)
    polls = (await db.execute(select(Poll).where(Poll.status == "open"))).scalars().all()
    closed = 0
    for poll in polls:
        due = poll.closes_at
        if due is not None and due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        if due is not None and due > now:
            continue
        try:
            await _close_one(db, poll)
            closed += 1
        except Exception:
            logger.warning("closing civic poll %s failed", poll.id, exc_info=True)
    return closed


async def _close_one(db, poll: Poll) -> None:
    """结票的唯一入口。事实层快照在这里统一作废(C1)。

    包一层是因为 :func:`_close_one_tally` 有五个终止分支(流会 / 未达门槛 /
    有效候选全灭 / 无人投票 / 正常胜出),每个分支都已经改过世界:``poll.status``
    首行就置 ``closed``(``open_polls`` 必变),胜出分支还可能经 ``_execute_outcome``
    换掉镇长或改掉政策。逐个分支贴一行 invalidate 迟早会漏一个,``finally`` 顺带
    把抛出路径也盖住 —— 多清一次的代价只是下一次读事实多查一遍库。
    """
    try:
        await _close_one_tally(db, poll)
    finally:
        _invalidate_town_facts()


def _invalidate_town_facts() -> None:
    """作废本进程的「小镇现况」快照(局部 import:事实层反过来 import 本模块的
    邻居 policy_service / election_service,模块级会成环)。

    整段 fail-open 且吞异常:它跑在 :func:`_close_one` 的 ``finally`` 里,让一次
    缓存清理去顶掉真正的结票异常是本末倒置。
    """
    try:
        from app.services.town_facts_service import invalidate_town_facts_cache
        invalidate_town_facts_cache()
    except Exception:  # pragma: no cover - 缓存清理不该反过来打断调用方
        logger.warning("town facts cache invalidation failed", exc_info=True)


async def _close_one_tally(db, poll: Poll) -> None:
    opts = list(poll.options_json or [])

    # E8 结票兜底：选项即人（mayor/office/duty）时校验那个人是否还在世界里。
    # 生产那张镇长选举的 4 个候选全部已被删除。光归零 npc_votes 不够——本分支
    # 让投票按钮首次真正出现在 DOM 里，一张真人 Vote 行就能让 argmax 落在一个
    # 不存在的人身上（`opts[win]["won"] = True` 从不看候选人是否存在）。所以
    # 除了归零，还把这类选项整体逐出胜者候选集（见下面的 dead_person_opts）。
    live_slugs = set((await db.execute(select(Resident.slug))).scalars().all())
    dead_person_opts: set[int] = set()
    for i, o in enumerate(opts):
        eff = (o or {}).get("effect")
        if not isinstance(eff, dict) or eff.get("type") not in _PERSON_TYPES:
            continue
        target = eff.get("slug")
        if target and target not in live_slugs:
            dead_person_opts.add(i)
            if int(o.get("npc_votes", 0)):
                logger.warning(
                    "poll %s option %d: candidate %r no longer exists — zeroing its "
                    "%d votes before the tally", poll.id, i, target, o["npc_votes"])
                o["npc_votes"] = 0

    rows = (await db.execute(
        select(Vote.option_idx, func.count()).where(Vote.poll_id == poll.id).group_by(Vote.option_idx)
    )).all()
    player_votes = {idx: n for idx, n in rows}
    tally = [
        int(o.get("npc_votes", 0)) + int(player_votes.get(i, 0))
        for i, o in enumerate(opts)
    ]
    poll.status = "closed"
    if not tally:
        await db.commit()
        return

    # 胜者只能从「候选人仍存在」的选项里选——一张真人票也不能把一个已被物理
    # 删除的人抬上「当选」。dead_person_opts 之外全是活选项（非人效果、或
    # slug 仍在 residents 表的人效果）。
    live_opts = [i for i in range(len(tally)) if i not in dead_person_opts]
    win = max(live_opts, key=lambda i: (tally[i], -i)) if live_opts else None

    if win is None:
        # 有效候选全灭：一个能当选的人都没有，归零也没意义——不写任何 won。
        poll.options_json = opts
        flag_modified(poll, "options_json")
        await db.commit()
        await _clerk_announce(
            db, f"镇务结果:{poll.question}",
            f"「{poll.question}」投票结束,有效候选均已不在名册上,本案流会。",
            civic_ref=f"poll_result:{poll.id}",
        )
        return

    # S2-5 track B: a tier-governed poll must clear its threshold (and, at the
    # absolute-majority tier, quorum) before the winner is executed. Gate off →
    # `verdict` is never computed and the pure-plurality path below runs
    # byte-for-byte as before S2-5.
    verdict = None
    if settings.polis_policy_approval_enabled:
        verdict = await _policy_threshold_verdict(db, opts, tally, win,
                                                  poll_id=poll.id)
    if verdict is not None:
        from app.services.policy_service import META_OUTCOME
        opts[0][META_OUTCOME] = verdict
        opts[win]["final_votes"] = tally[win]
        poll.options_json = opts
        flag_modified(poll, "options_json")
        await db.commit()
        await _clerk_announce(
            db, f"镇务结果:{poll.question}",
            f"「{poll.question}」投票结束,「{opts[win]['label']}」得 {tally[win]} 票,"
            f"{_VERDICT_NOTE.get(verdict, '未达生效条件')},本案流会,政策维持原状。",
            civic_ref=f"poll_result:{poll.id}",
        )
        return

    if tally[win] <= 0:
        # 纯 plurality 路径今天没有 no_votes 保护（那是 _policy_threshold_verdict
        # 的专利，且只在 polis_policy_approval_enabled=True 时才跑）——零票不该
        # 有胜者，index 0 的 argmax 兜底会把「谁都没投」说成「klaus 以 0 票胜出」。
        poll.options_json = opts
        flag_modified(poll, "options_json")
        await db.commit()
        await _clerk_announce(
            db, f"镇务结果:{poll.question}",
            f"「{poll.question}」投票结束,无人投票,本案流会。",
            civic_ref=f"poll_result:{poll.id}",
        )
        return

    opts[win]["won"] = True
    opts[win]["final_votes"] = tally[win]
    poll.options_json = opts
    flag_modified(poll, "options_json")
    await db.commit()

    effect = opts[win].get("effect")
    result_note = f"「{poll.question}」投票结束,「{opts[win]['label']}」以 {tally[win]} 票胜出。"
    # 赢家在 install_mayor 里已经写过第一人称那条记忆（election_service 的
    # mayor install side-effects），再收一条第三人称的广播就是同一件事在同一
    # 个人脑子里记两遍。只在真装上了（applied）之后才排除——流会分支没有赢家。
    exclude_id = None
    if effect:
        applied = await _execute_outcome(db, effect, poll_id=poll.id)
        if applied:
            result_note += "议案已生效。"
            if effect.get("type") == "mayor" and effect.get("slug"):
                exclude_id = (await db.execute(
                    select(Resident.id).where(Resident.slug == effect["slug"])
                )).scalar_one_or_none()
        elif (effect.get("type") == "mayor"
              and await _winner_lost_civic_rights(db, effect.get("slug"))):
            # F2: install_mayor 的结票复核不通过 —— 当选人在投票窗口内被撤销了
            # 公民权（或已不在名册上）。它是零写入的 return False，所以本案只是
            # 流会，不是「生效时出了问题」。
            result_note += f"{_VERDICT_NOTE['winner_ineligible']},本案流会。"
        else:
            result_note += "议案生效时遇到问题,已记录。"
    await _clerk_announce(db, f"镇务结果:{poll.question}", result_note,
                          civic_ref=f"poll_result:{poll.id}",
                          exclude_resident_id=exclude_id)


#: 流会原因 → 公告措辞（世界内信息物；探针数值永不进 NPC prompt）。
_VERDICT_NOTE = {
    "threshold_not_met": "未达本级审批所需的票数门槛",
    "quorum_not_met": "投票人数未达法定出席门槛",
    "no_votes": "无人投票",
    # F2：install_mayor 结票复核不通过（当选人在投票窗口内失去了公民资格）。
    #     只有 :func:`_winner_lost_civic_rights` 复核确认后才说得出口。
    "winner_ineligible": "当选人已失去公民资格",
}


async def _winner_lost_civic_rights(db, slug: str | None) -> bool:
    """结票复核的**复核**：仅当 ``slug`` 指名的居民确实不在政治权利集合里时
    才返回 True。

    ``install_mayor`` 的 ``return False`` 不止「不合格」一种原因——写入故障也被
    本模块 :func:`_execute_outcome` 的 ``except Exception`` 吞成 ``False``。若按
    effect 类型无条件归因，一次基础设施故障就会被翻译成对一位具名角色的名誉
    裁决；``BulletinPost`` 不进 NPC prompt / memory，但会经
    ``app/routers/bulletin.py`` 永久呈现在玩家 UI 上，是世界内的假信息。

    两条兜底一律返回 False（= 不下这个裁决，回落到通用措辞）：

    - ``slug`` 为空：没有具名对象，说什么都是冤枉；
    - 查询本身出错：session 可能已经因为前一步的故障进了 aborted 状态。措辞
      助手永远不得反过来掀翻结票（本模块通行的 fail-open）。

    代价是选举结票路径上多一次 SELECT，且只在 ``applied is False`` 的分支里跑。
    """
    if not slug:
        return False
    try:
        return int((await db.execute(
            select(func.count()).select_from(Resident).where(
                Resident.slug == slug, Resident.is_civic_voter)
        )).scalar() or 0) == 0
    except Exception:
        logger.warning("winner eligibility re-check failed for %r — falling "
                       "back to the generic wording", slug, exc_info=True)
        return False


async def _eligible_voter_count(db) -> int:
    """Quorum denominator: the residents who could have voted.

    Must track the ballot query above exactly — a denominator wider than the
    electorate silently raises the bar every poll has to clear.
    """
    return int((await db.execute(
        select(func.count()).select_from(Resident).where(
            Resident.is_civic_voter)
    )).scalar() or 0)


async def _policy_threshold_verdict(db, opts: list[dict], tally: list[int],
                                    win: int, *,
                                    poll_id: str | None) -> str | None:
    """S2-5 §2 任务 4 — threshold / quorum judgement for a tier-governed poll.

    Returns ``None`` when the poll may execute (either it carries no tier
    metadata at all — an ordinary civic poll keeps pure plurality — or the
    winner cleared its bar), otherwise a 流会 reason code.

    ``poll_id`` is keyword-only and **required** (it may be ``None`` only for a
    poll that genuinely has no id yet): the sole thing this function emits
    besides its return value is the empty-electorate WARNING below, and an
    operator who cannot tell *which* poll it fired on has no trail at all.

    F2 冻结分母：法定人数的分母取 **开票那一刻** 的快照
    (``options_json[0][META_ELIGIBLE_AT_OPEN]``，由 :func:`propose` 写入)，
    而不是结票时的实时 :func:`_eligible_voter_count`。适用面：整段只在
    ``polis_policy_approval_enabled`` 为真（``_close_one`` 的 gate）、且 opts[0]
    带 ``META_THRESHOLD`` 时才计算；quorum 还要额外带 ``META_QUORUM``。普通
    civic poll 与镇长选举 poll 走纯 plurality，分母不参与判决——撤销对它们的
    影响是票差而非流会。
    """
    from app.services.policy_service import META_THRESHOLD, META_QUORUM

    blob = opts[0] if opts else {}
    threshold = blob.get(META_THRESHOLD)
    if threshold is None:
        return None                      # not a policy poll → status quo
    total = sum(tally)
    if total <= 0:
        return "no_votes"
    if blob.get(META_QUORUM):
        # F2: 分母取开票那一刻的快照；存量 poll（本改动之前开的）没有快照，
        # 回落实时计数 —— 行为与改动前逐字节一致。
        frozen = blob.get(META_ELIGIBLE_AT_OPEN)
        eligible = int(frozen if frozen is not None
                       else await _eligible_voter_count(db))
        if eligible <= 0:
            # 行为不变（跳过法定人数判定），但不再是一句沉默的 `eligible > 0`
            # 短路：安全阀在分母为 0 时自己关掉，语义上说不通，至少要留痕。
            logger.warning(
                "poll %s: quorum check skipped, eligible electorate is %d "
                "(frozen=%r) — an empty electorate makes the quorum "
                "denominator meaningless",
                poll_id, eligible, frozen)
        elif total < eligible * settings.polis_policy_quorum_fraction:
            return "quorum_not_met"
    if (tally[win] / total) < float(threshold):
        return "threshold_not_met"
    return None


async def _execute_outcome(db, effect: dict, *, poll_id: int | None = None) -> bool:
    """Land a winning outcome through an existing channel. Returns success."""
    etype = effect.get("type")
    try:
        if etype == "policy" and settings.polis_policy_approval_enabled:
            # S2-5: the only new effect type. Gated — with the approval gate off
            # this branch is not even evaluated, so a "policy" effect falls
            # through to the `return False` below exactly like any unknown type
            # did before S2-5 (byte-level status quo).
            from app.services.policy_service import (
                PolicyService, PolicyImmutableError,
            )
            try:
                return await PolicyService(db).apply_amend(
                    effect["key"], effect["value"],
                    expected_version=effect.get("expected_version"),
                    updated_by=f"poll:{poll_id if poll_id is not None else '?'}",
                )
            except PolicyImmutableError:
                # A core entry can never be amended, not even by referendum.
                logger.warning("civic outcome targeted a constitutional_core "
                               "policy (%s) — refused", effect.get("key"))
                return False
        if etype == "system_config":
            from app.services.config_service import ConfigService
            await ConfigService(db).set(
                effect["key"], effect["value"],
                group=effect.get("group", "civic"), updated_by="civic_vote",
            )
            return True
        if etype == "dynamic_location":
            return await _add_dynamic_location(db, effect["data"])
        if etype == "narrative":
            from app.models.world_event import WorldEvent
            ev = effect.get("event", {})
            now = datetime.now(UTC)
            db.add(WorldEvent(
                type="news", title=ev.get("title", "镇务公告"),
                description=ev.get("description", ""),
                payload_json=ev.get("payload", {}),
                starts_at=now, ends_at=now + timedelta(days=ev.get("days", 1)),
                is_active=False,
            ))
            await db.commit()
            return True
        if etype == "mayor":
            from app.services.election_service import install_mayor
            return await install_mayor(db, effect.get("slug"))
    except Exception:
        logger.warning("civic outcome execution failed (%s)", etype, exc_info=True)
    return False


async def _add_dynamic_location(db, data: dict) -> bool:
    """Insert a dynamic_locations overlay row + trigger the world reload so the
    new building is reachable without a redeploy."""
    from app.models.dynamic_location import DynamicLocation
    slug = data.get("slug")
    if not slug or "bounds" not in data:
        return False
    if settings.civic_build_schema_enabled:
        # routers/polls.py:92-98 允许 admin 直接塞任意 effect dict,所以净化必须
        # 挂在落库点而不是 CIVIC_AGENDA 侧。丢键不拒条:拒绝会让「新字段先落库、
        # 代码后上线」的部署顺序把合法行判成非法。
        from app.services.civic_build import normalize_location_data
        data, _schema_warns = normalize_location_data(data)
        for _w in _schema_warns:
            logger.warning("civic build payload normalized (%s): %s", slug, _w)
    existing = (await db.execute(
        select(DynamicLocation).where(DynamicLocation.slug == slug)
    )).scalar_one_or_none()
    payload = {k: v for k, v in data.items() if k != "slug"}
    if existing is None:
        db.add(DynamicLocation(slug=slug, data_json=payload, active=True))
    else:
        existing.data_json = payload
        existing.active = True
    await db.commit()
    try:
        from app.lab.apply import reload_world, publish_world_reload
        await reload_world()
        await publish_world_reload()
    except Exception:
        logger.warning("world reload after civic build failed", exc_info=True)
    return True


# ── F3.3: public lecture → resident debate ─────────────────────────────

async def maybe_spawn_lecture_debate(db, event: dict) -> bool:
    """When a lecturer's public-lecture event ends, spin up a resident debate on
    the lecture topic between two socially-active, SBTI-contrasting residents.
    Best-effort; returns True if a debate was created."""
    if not settings.civic_polls_enabled:
        return False
    payload = event.get("payload_json") or {}
    if payload.get("duty") != "lecturer":
        return False
    try:
        residents = (await db.execute(
            select(Resident).where(Resident.is_autonomous)
        )).scalars().all()
        # socially active (So1 != L), lecturer excluded
        # S10:讲席的判定走 duty_service.duty_key(按人读营生的唯一入口),不手写
        # meta_json 链——这里是逐人过滤,不是「按 key 反查持有人」,所以不该用
        # find_duty_resident(那条路在 polis_office_enabled 时先查 offices)。
        from app.services.duty_service import duty_key as _duty_key
        pool = []
        for r in residents:
            dims = (r.meta_json or {}).get("sbti", {}).get("dimensions", {})
            if dims.get("So1") == "L":
                continue
            if _duty_key(r) == "lecturer":
                continue
            pool.append(r)
        if len(pool) < 2:
            return False
        # contrast on A1 (worldview): pick one optimistic, one skeptical if we can
        hi = next((r for r in pool if (r.meta_json or {}).get("sbti", {})
                   .get("dimensions", {}).get("A1") == "H"), None)
        lo = next((r for r in pool if (r.meta_json or {}).get("sbti", {})
                   .get("dimensions", {}).get("A1") == "L"), None)
        a = hi or pool[0]
        b = lo or next((r for r in pool if r.id != a.id), None)
        if b is None or a.id == b.id:
            return False
        topic = event.get("title", "小镇议题").replace("的公开课", "")
        from app.services.debate_service import create_debate
        await create_debate(db, f"关于「{topic}」的争论", a.slug, b.slug)
        return True
    except Exception:
        logger.warning("lecture debate spawn failed", exc_info=True)
        return False


# ── helper ─────────────────────────────────────────────────────────────

async def _clerk_announce(db, title: str, body: str, *,
                          civic_ref: str | None = None,
                          exclude_resident_id: str | None = None) -> None:
    """镇务公告的唯一出口:公告板一份,全体自治居民的记忆一份(S7)。

    **广播只挂在这一处。** ``_close_one`` → ``_execute_outcome`` →
    ``install_mayor`` 是一条嵌套调用链,三处各广播一次会让一次选举给每人写 3
    条 —— ``_fetch_event_candidates`` 只静态截前 30 条
    (``app/memory/service.py``),镇务记忆会反过来把个人记忆挤出候选池。这里是
    ``_close_one`` 全部终止分支(流会 / 未达门槛 / 无人投票 / 正常胜出)与
    :func:`propose` 开票征询的唯一汇合点,收敛到一处既不漏也不重。

    ``civic_ref`` 必须是调用方给的**稳定值**(``poll_result:{poll.id}`` /
    ``poll_open:{poll.id}``)。回落值 ``post:{post.id}`` 只服务还没有稳定键的
    临时调用点:公告行主键每次补跑都是新 uuid,拿它当幂等键等于没有幂等。

    分档 importance:结果类(``poll_result:``)是「这件事定了」,进最高档;征询与
    日常公告是「镇上在议一件事」,走 notice 档。两档都可在 S1 的闸门里调。
    """
    try:
        from app.services.bulletin_service import create_post
        from app.services.duty_service import find_duty_resident
        clerk = await find_duty_resident(db, "town_clerk")
        author_id = clerk.id if clerk else None
        post = await create_post(db, "notice", title, body, author_resident_id=author_id)
        # 闸关时 broadcast_civic_memory 零查询零写入直接返 0,整条链逐字节不变。
        from app.services.civic_memory import broadcast_civic_memory
        await broadcast_civic_memory(
            db, body, kind="civic", ref=civic_ref or f"post:{post.id}",
            importance=(settings.civic_memory_importance
                        if civic_ref and civic_ref.startswith("poll_result:")
                        else settings.civic_memory_notice_importance),
            exclude_resident_id=exclude_resident_id,
        )
    except Exception:
        logger.warning("civic clerk announce failed", exc_info=True)
