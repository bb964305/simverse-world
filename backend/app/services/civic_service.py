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
    await _clerk_announce(
        db,
        f"镇务征询:{topic}",
        f"现就「{topic}」公开征询全镇意见,选项:{'、'.join(o['label'] for o in opts)}。"
        f"{proposer_line}投票于 {poll.closes_at.date()} 截止,请各位居民踊跃参与。",
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
        select(Resident).where(Resident.is_autonomous)
    )).scalars().all()
    if not residents:
        return 0

    from app.services import relation_service
    by_slug = {r.slug: r for r in residents}
    cast = 0
    for poll in polls:
        opts = list(poll.options_json or [])
        if not opts:
            continue
        voters = set(poll.options_json[0].get("_npc_voters", [])) if opts else set()
        for r in residents:
            if r.slug in voters:
                continue
            idx = await _npc_choice(db, r, poll, opts, relation_service, by_slug)
            opts[idx]["npc_votes"] = int(opts[idx].get("npc_votes", 0)) + 1
            voters.add(r.slug)
            cast += 1
        opts[0]["_npc_voters"] = sorted(voters)
        poll.options_json = opts
        flag_modified(poll, "options_json")
    if cast:
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
    duty = (resident.meta_json or {}).get("duty", {}).get("key")
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
    duty = (resident.meta_json or {}).get("duty", {}).get("key")

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
    opts = list(poll.options_json or [])
    # player votes from the votes table
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
    win = max(range(len(tally)), key=lambda i: (tally[i], -i))

    # S2-5 track B: a tier-governed poll must clear its threshold (and, at the
    # absolute-majority tier, quorum) before the winner is executed. Gate off →
    # `verdict` is never computed and the pure-plurality path below runs
    # byte-for-byte as before S2-5.
    verdict = None
    if settings.polis_policy_approval_enabled:
        verdict = await _policy_threshold_verdict(db, opts, tally, win)
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
        )
        return

    opts[win]["won"] = True
    opts[win]["final_votes"] = tally[win]
    poll.options_json = opts
    flag_modified(poll, "options_json")
    await db.commit()

    effect = opts[win].get("effect")
    result_note = f"「{poll.question}」投票结束,「{opts[win]['label']}」以 {tally[win]} 票胜出。"
    if effect:
        applied = await _execute_outcome(db, effect, poll_id=poll.id)
        result_note += "议案已生效。" if applied else "议案生效时遇到问题,已记录。"
    await _clerk_announce(db, f"镇务结果:{poll.question}", result_note)


#: 流会原因 → 公告措辞（世界内信息物；探针数值永不进 NPC prompt）。
_VERDICT_NOTE = {
    "threshold_not_met": "未达本级审批所需的票数门槛",
    "quorum_not_met": "投票人数未达法定出席门槛",
    "no_votes": "无人投票",
}


async def _eligible_voter_count(db) -> int:
    """Quorum denominator: the NPC residents who could have voted."""
    return int((await db.execute(
        select(func.count()).select_from(Resident).where(
            Resident.is_autonomous)
    )).scalar() or 0)


async def _policy_threshold_verdict(db, opts: list[dict], tally: list[int],
                                    win: int) -> str | None:
    """S2-5 §2 任务 4 — threshold / quorum judgement for a tier-governed poll.

    Returns ``None`` when the poll may execute (either it carries no tier
    metadata at all — an ordinary civic poll keeps pure plurality — or the
    winner cleared its bar), otherwise a 流会 reason code.
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
        eligible = await _eligible_voter_count(db)
        if eligible > 0 and total < eligible * settings.polis_policy_quorum_fraction:
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
        pool = []
        for r in residents:
            dims = (r.meta_json or {}).get("sbti", {}).get("dimensions", {})
            if dims.get("So1") == "L":
                continue
            if (r.meta_json or {}).get("duty", {}).get("key") == "lecturer":
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

async def _clerk_announce(db, title: str, body: str) -> None:
    try:
        from app.services.bulletin_service import create_post
        from app.services.duty_service import find_duty_resident
        clerk = await find_duty_resident(db, "town_clerk")
        author_id = clerk.id if clerk else None
        await create_post(db, "notice", title, body, author_resident_id=author_id)
    except Exception:
        logger.warning("civic clerk announce failed", exc_info=True)
