"""Seasonal mayor election (M6) — built on the M3 civic-poll engine.

An election is a civic poll whose options are candidate residents; each option
carries a ``{"type": "mayor", "slug": ...}`` effect. Voting (NPC rule-based +
player) and closing reuse ``civic_service`` verbatim. When the poll closes the
winner is installed as mayor:

  - ``meta_json['mayor'] = True`` on the winner (and cleared on everyone else),
    which the wage path reads for the town-wide bonus — no extra query;
  - ``current_mayor`` recorded in system_config for provenance;
  - the clerk posts the result, and the winner's bulletins carry a mayor badge
    implicitly via authorship.

Gated by ``settings.election_enabled``. Fail-open.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, UTC

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.models.resident import Resident
from app.models.system_config import SystemConfig

logger = logging.getLogger(__name__)

ELECTION_TAG = "镇长选举"


async def open_election(db, *, candidate_slugs: list[str] | None = None, days: int | None = None):
    """Open a mayor election poll. Candidates default to ambitious, socially
    active NPCs (SBTI Ac1=H or So1=H). Returns the Poll or None."""
    if not settings.election_enabled:
        return None
    from app.services import civic_service

    residents = (await db.execute(
        select(Resident).where(Resident.is_civic_voter)
    )).scalars().all()
    by_slug = {r.slug: r for r in residents}

    if candidate_slugs:
        candidates = [by_slug[s] for s in candidate_slugs if s in by_slug]
    else:
        candidates = [
            r for r in residents
            if _dim(r, "Ac1") == "H" or _dim(r, "So1") == "H"
        ]
        if len(candidates) < 2:  # fallback: highest-heat residents
            candidates = sorted(residents, key=lambda r: r.heat or 0, reverse=True)[:3]
    # F1 第 2 项:声誉**不参与**候选集选取。此处曾按声誉排序再 [:4],等于把「被
    # 议论最多、叙事最中心」的居民系统性挤出候选(tone 恒为负 → 被议论就扣分)。
    # 被动选举权不因名声受损而剥夺;声誉的唯一入票通道是 civic_service._npc_choice
    # 里的 vote_trust_delta(),影响得票而不决定谁能参选。
    # 候选集口径回到 S1-1 之前:SBTI(Ac1/So1=H)优先,不足 2 人回落 heat 前三。
    candidates = candidates[:4]
    if len(candidates) < 2:
        return None

    options = [
        {"label": c.name, "effect": {"type": "mayor", "slug": c.slug}}
        for c in candidates
    ]
    return await civic_service.propose(
        db, f"{ELECTION_TAG}:谁来当下一任镇长?", options, days=days,
    )


async def maybe_open_seasonal_election(db):
    """Nightly trigger (M6): open a mayor election when one is due.

    Cadence rules (all state in system_config — survives restarts, no schema):
      - never while an election poll is already open;
      - a season becoming active holds an election once per season
        (``election_last_season`` remembers the season already served);
      - off-season, elections repeat every ``election_interval_days``
        (``election_last_opened`` stores the last open date).

    Returns the opened Poll or None. Fail-open: any error means "not tonight".
    """
    if not (settings.election_enabled and settings.civic_polls_enabled):
        return None
    from app.models.season import Poll
    open_poll = (await db.execute(
        select(Poll).where(
            Poll.status == "open", Poll.question.like(f"{ELECTION_TAG}%"),
        )
    )).scalars().first()
    if open_poll is not None:
        return None

    from app.services.config_service import ConfigService
    cs = ConfigService(db)

    season = None
    try:
        from app.services.season_service import get_active_season
        season = await get_active_season(db)
    except Exception:
        logger.warning("active-season lookup failed for election", exc_info=True)

    if season is not None:
        if await cs.get("election_last_season") == season.id:
            return None
        poll = await open_election(db)
        if poll is not None:
            await cs.set("election_last_season", season.id,
                         group="civic", updated_by="election")
            await cs.set("election_last_opened", datetime.now(UTC).date().isoformat(),
                         group="civic", updated_by="election")
        return poll

    today = datetime.now(UTC).date()
    last = await cs.get("election_last_opened")
    if last:
        try:
            from datetime import date as _date
            elapsed = (today - _date.fromisoformat(str(last))).days
            if elapsed < settings.election_interval_days:
                return None
        except ValueError:
            logger.warning("unparseable election_last_opened %r — reopening", last)
    poll = await open_election(db)
    if poll is not None:
        await cs.set("election_last_opened", today.isoformat(),
                     group="civic", updated_by="election")
    return poll


async def _record_current_mayor(db, slug: str) -> None:
    """Stage ``system_config['current_mayor'] = slug`` in the *caller's*
    transaction — deliberately not ``ConfigService.set()``, which commits
    (``config_service.py:48``) and would split the install back into two
    transactions. Value encoding is byte-identical to ConfigService's
    (``json.dumps``), so ``ConfigService.get`` keeps reading it.
    """
    cfg = (await db.execute(
        select(SystemConfig).where(SystemConfig.key == "current_mayor")
    )).scalar_one_or_none()
    if cfg is None:
        db.add(SystemConfig(key="current_mayor", value=json.dumps(slug),
                            group="civic", updated_by="election"))
    else:
        cfg.value = json.dumps(slug)
        cfg.updated_by = "election"
        cfg.updated_at = datetime.now(UTC)


async def install_mayor(db, slug: str | None) -> bool:
    """Set ``slug`` as the sitting mayor (clearing any previous one) and record
    it in system_config. Returns True on success.

    F2 收口的两条语义（原实现的两个坑）：

    1. **结票时复核资格**。winner 用 ``Resident.is_civic_voter``（政治权利）
       解析，不是 ``is_autonomous``（世界人口）——候选名单是开票那一刻的快照，
       中途可能有人被降级，**快照不构成信任**。不合格立即 ``return False`` 且
       零写入（三个表示 meta_json / system_config / offices 一个都不碰），由
       ``civic_service._close_one`` 走流会公告分支。
    2. **事务化**。原实现先 ``commit()`` 再判 ``winner is None``：winner 解析
       失败时旧镇长的 ``meta_json`` 已被清掉、``system_config`` 与 offices 却
       仍指向他，留下三向分歧（触发条件今天就可达：目标 slug 查不到即可）。
       现在旧镇长清理、新镇长安装、``current_mayor`` 记录在同一个 SAVEPOINT +
       同一次 commit 里，失败时**自己把 savepoint 回掉**——调用方会把异常吞掉
       并紧接着为公告 ``commit()``，留在 session 里的脏对象会被那次 commit
       顺手落盘（判据见函数体内的注释）。

    清扫面是「全表带 ``meta_json`` 的居民」而不是 ``is_autonomous``——通用约束：
    凡是清理「已离开集合 S 的居民」的扫描，都不能用 S 本身做 WHERE（逐出档
    落地时无需回来改这里）。
    """
    if not slug:
        return False

    winner = (await db.execute(
        select(Resident).where(Resident.slug == slug, Resident.is_civic_voter)
    )).scalar_one_or_none()
    if winner is None:
        logger.warning(
            "install_mayor refused: %r is not (or is no longer) a civic voter "
            "— zero writes, the poll fails over to the 流会 branch", slug)
        return False

    # 两个表示的写入跑在同一个 SAVEPOINT 里。**必须是 savepoint 而不是顶层
    # ``db.rollback()``**：本函数的直接调用方 ``civic_service._execute_outcome``
    # 把异常吞成 False，紧接着 ``_close_one`` 用它自己更早加载的 ``poll`` 拼公告
    # 标题（``civic_service.py`` 的 ``poll.question``）。顶层 rollback 会
    # ``_restore_snapshot(dirty_only=False)`` expire **整个** identity map，
    # 包含那个本函数从没碰过的 ``poll``，那次同步属性读随即在没有 greenlet
    # 上下文的地方炸 ``MissingGreenlet``（已实测复现，判据同
    # ``civic_membership.revoke_citizenship`` 的「异常路径调用方契约」）。
    # savepoint 的自动回滚只 ``_restore_snapshot(dirty_only=True)``，旁观对象
    # 不受影响。
    #
    # 而回滚本身不能省：调用方吞掉异常后 ``_clerk_announce`` → ``create_post``
    # 会自己 ``commit()``（``bulletin_service.py``），留在 session 里的清扫
    # 写入会被那次 commit 顺手落盘，「同一次 commit」的保证就破了。
    async with db.begin_nested():
        # ⚠️ 这个 WHERE **几乎不筛掉任何行**，别误读成一次有意义的窄化：
        # SQLAlchemy 的 ``JSON`` 类型默认 ``none_as_null=False``，Python ``None``
        # 被序列化成 JSON 字面量 ``null`` 而不是 SQL NULL，所以 ``meta_json``
        # 为空的居民这一列也是 ``'null'``、``IS NOT NULL`` 依然为真（实测：
        # 两行两命中）。留着它是因为它是所需集合的**超集**——扫多了无害（多一次
        # 无 mayor 键的 dict 拷贝），扫漏了才会留下第二个镇长。要点是**不能**
        # 换成 ``is_autonomous`` 之类的成员谓词：那是「用集合 S 去清理刚离开 S
        # 的人」。
        others = (await db.execute(
            select(Resident).where(Resident.meta_json.isnot(None))
        )).scalars().all()
        for r in others:
            if r.slug == slug:
                continue
            meta = dict(r.meta_json or {})
            if meta.pop("mayor", None) is not None:
                r.meta_json = meta
                flag_modified(r, "meta_json")
        winner_meta = dict(winner.meta_json or {})
        if not winner_meta.get("mayor"):
            winner_meta["mayor"] = True
            winner.meta_json = winner_meta
            flag_modified(winner, "meta_json")

        await _record_current_mayor(db, slug)
    await db.commit()

    # S2-1: dual-write the offices row when the gate is on. Both legacy
    # stores above stay alive — meta_json['mayor'] is the wage multiplier
    # (gotcha #1), system_config the read fallback. Fail-open: an offices
    # hiccup must never break an election result.
    if settings.polis_office_enabled:
        try:
            from app.services.office_service import OfficeService
            await OfficeService(db).appoint(
                "mayor", slug, fill_strategy="election",
                term_days=settings.polis_office_mayor_term_days,
            )
        except Exception:
            logger.warning("office dual-write failed for mayor", exc_info=True)

    # C1: 镇长换人了 —— 作废本进程的「小镇现况」快照,否则同一个 worker 里的
    # NPC 最长还要说 civic_facts_cache_ttl_seconds 那么久的旧镇长(本批修的正
    # 是这类幻觉)。位置在两个表示都落库、offices 双写也做完之后:早于它们清等
    # 于给下一次读留一个「刚清完又被旧值填回去」的窗口。跨进程仍受 TTL 约束
    # (invalidate_town_facts_cache 的 docstring 写明了这条取舍)。
    # 局部 import:town_facts_service 模块级 import 本模块,反向模块级会成环。
    try:
        from app.services.town_facts_service import invalidate_town_facts_cache
        invalidate_town_facts_cache()
    except Exception:  # pragma: no cover - 缓存清理不该掀翻一次选举结果
        logger.warning("town facts cache invalidation failed", exc_info=True)

    try:
        from app.services.feed_service import push
        await push(slug, "goal_achieved", {"goal": "当选小镇镇长"})
        from app.memory.service import MemoryService
        await MemoryService(db).add_memory(
            winner.id, "event",
            "我当选了小镇的镇长。这份信任沉甸甸的,得对得起投我票的每一个人。",
            0.9, "reflection",
        )
    except Exception:
        logger.warning("mayor install side-effects failed", exc_info=True)
    return True


async def current_mayor(db) -> str | None:
    # S2-1: offices-backed read when the gate is on — offices is the new
    # authority, system_config the fallback (pre-backfill worlds, or a
    # vacant office after term expiry with a legacy value already cleared
    # by term_check). Gate off → byte-level legacy behavior.
    if settings.polis_office_enabled:
        try:
            from app.services.office_service import OfficeService
            holder = await OfficeService(db).get_holder("mayor")
            if holder:
                return holder
        except Exception:
            logger.warning("offices-backed current_mayor read failed", exc_info=True)
    from app.services.config_service import ConfigService
    try:
        return await ConfigService(db).get("current_mayor")
    except Exception:
        return None


def _dim(resident, code: str) -> str:
    return (resident.meta_json or {}).get("sbti", {}).get("dimensions", {}).get(code, "M")
