"""E9 辩论擂台 (debate arena).

Lifecycle: announced → live → voting → settled.

- announced: staking open (players back side a/b, 10-200 🪙, one stake each,
  the stake auto-counts as a vote for that side).
- live: two residents argue for ``ROUNDS`` turns via the system LLM channel;
  each turn is broadcast as a ``debate_turn`` WS frame. If the LLM fails
  mid-debate the debate auto-draws and every stake is refunded in full.
- voting: free votes open (one per user, deduped in Redis).
- settled: the majority side wins. The losing pool is split among winning
  stakers pro-rata after a 5% burn; the winner residents' mood lifts (E1) and
  both residents keep a memory. A tie refunds every stake in full.

Settlement money is authoritative from the ``debate_stakes`` rows, not the
cached ``pool_a``/``pool_b`` counters, so a lost counter update can never
mint or destroy coins.
"""

import hashlib
import logging
from datetime import datetime, timedelta, UTC

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from app.models.debate import Debate, DebateStake
from app.models.resident import Resident

logger = logging.getLogger(__name__)

STAKE_MIN = 10
STAKE_MAX = 200
BURN_RATE = 0.05  # 5% of the losing pool is burned on payout
ROUNDS = 6

#: 观众收益的人数上限。关系 bump 是两两 O(n²)(8 人 = 28 次带 commit 的 UPDATE),
#: 但这条路径每场辩论只在 settle 时跑一次、不在 tick 热路径上,所以不需要
#: market_day_crowd_cohort 那套 TTL 缓存 + 单飞锁。
AUDIENCE_LIMIT = 8

#: 观众的四层收益系数。全部是既有系统的既有量纲,没有一项是货币:
#: importance 照 _resident_aftermath 里辩手的 0.7/0.6 降一档(design §②-c ≈0.5);
#: 心情照赢家的 +0.3/+0.1 降一档;social 是 needs 的 [0,1] 标度;
#: familiarity 是 relation_service 的 [0,1] 标度。
AUDIENCE_MEMORY_IMPORTANCE = 0.5
AUDIENCE_MOOD_VALENCE = 0.1
AUDIENCE_MOOD_AROUSAL = 0.05
AUDIENCE_SOCIAL_RESTORE = 0.15
AUDIENCE_FAMILIARITY = 0.02

#: 反查场地时扫描的 script 事件行数上限。script 事件只有两个产地(#7 的 live 辩论、
#: #8 的公开课),而 settle 发生在 run_live 之后 debate_vote_window_min 内,目标行
#: 必在最新的一批里。上限存在只是为了让这条查询恒定代价。
_VENUE_SCAN_LIMIT = 200

_VOTERS_KEY = "sv:debate_voters:{id}"

#: run_live 把辩论推进到 voting 的真实时刻。debates 表没有相位时间列，而本
#: 批次不动 schema（红线：迁移与行为变更不得同一次变更）。settle 的判据必须
#: 是「投票开了多久」而不是「辩论建了多久」——否则一场积压的 announced 辩论
#: 会在同一轮里被 run_live 之后立刻 settle，玩家一票都投不上，结果恒为平局。
#: Redis 丢失（重启 / 本驱动器上线前就已在 voting 的历史数据）时回落到
#: starts_at 推算，退化成保守结算而不是卡死。
_VOTING_SINCE_KEY = "sv:debate_voting_since:{id}"
_VOTING_SINCE_TTL = 7 * 86400

#: 辩论的上演场地。debates 表 13 列无 location,而本批次不动 schema(红线:迁移与
#: 行为变更不得同一次变更),所以 create_debate 收到的 venue 走 Redis 传给 run_live
#: —— 与上面 _VOTING_SINCE_KEY 同一条思路、同一个失败姿势。
#: 读不到 = **不建事件**(fail-closed),绝不臆造场地:announced → live 只隔
#: debate_stake_window_min(默认 30 分钟),要在这个窗口里丢 Redis 才会漏掉一场的
#: 人流拉力,代价上限是一场辩论没观众;而回落到「随便挑一个 stage 地点」会把全镇
#: 往错的楼里拉。
_VENUE_KEY = "sv:debate_venue:{id}"
_VENUE_TTL = 7 * 86400

DEBATE_SYSTEM = (
    "你正在参加一场辩论擂台。你要为自己的立场辩护，语气鲜明、有理有据，"
    "针对上一位发言者的观点回应。60 字以内，只输出你的发言。"
)


class DebateError(Exception):
    """Invalid debate request (router maps to 400)."""


# --------------------------------------------------------------------------- #
# Setup / staking                                                             #
# --------------------------------------------------------------------------- #
async def create_debate(db, topic: str, a_slug: str, b_slug: str,
                        *, venue: str | None = None) -> Debate:
    d = Debate(topic=topic, resident_a_slug=a_slug, resident_b_slug=b_slug, status="announced")
    db.add(d)
    await db.commit()
    await db.refresh(d)
    # P2 #7:上演场地。venue 为 None(今天所有调用方的默认)时整段跳过,与改前逐字节
    # 相同。场地不进 schema —— 见 _VENUE_KEY 的说明。
    if venue:
        await _remember_venue(d.id, venue)
    # S1-3: seed opposing issue stances for the two debaters. `announced` is
    # the reliable first-hand signal — the debate lifecycle stops here today
    # (no live/settle driver in app code). Best-effort + gated: an opinion
    # failure must never break debate creation.
    try:
        from app.config import settings
        if settings.polis_opinion_enabled:
            from app.services.opinion_service import OpinionService
            await OpinionService(db).update_from_debate(d, seed_only=True)
    except Exception:
        logger.warning("opinion seed from create_debate failed", exc_info=True)
    return d


async def _remember_venue(debate_id: str, venue: str) -> None:
    """记下这场辩论的上演场地。只有声明了 stage 能力的地点才算数。

    校验放在写入侧:写入侧知道调用方是谁(civic_service 的公开课链),读取侧只拿得到
    一个字符串。地点没声明 stage 就当没给场地 —— 存量 dynamic_locations 行没有
    capabilities 键时天然走这条路,缺省安全。
    """
    from app.agent.location_caps import CAP_STAGE
    from app.agent.map_data import has_capability
    if not has_capability(venue, CAP_STAGE):
        logger.debug("debate venue %r does not declare stage; ignored", venue)
        return
    try:
        from app.redis_client import get_redis
        await get_redis().set(_VENUE_KEY.format(id=debate_id), venue, ex=_VENUE_TTL)
    except Exception:
        # 场地是叙事装饰;辩论本体(玩家能押注的那个对象)不能因它建不出来。
        logger.warning("debate venue mark failed for %s", debate_id, exc_info=True)


async def _debate_venue(debate_id: str) -> str | None:
    """读回上演场地;没记过 / 读不出来 / 地点已不再声明 stage → None。

    读取侧**再校验一次**:Redis 里的值可能是几天前写的,而能力声明是公投随时能改的
    数据。两侧都判,任一侧不成立就退化成「没有场地」= 今天的行为。
    """
    try:
        from app.redis_client import get_redis
        raw = await get_redis().get(_VENUE_KEY.format(id=debate_id))
    except Exception:
        logger.warning("debate venue read failed for %s", debate_id, exc_info=True)
        return None
    if not raw:
        return None
    from app.agent.location_caps import CAP_STAGE
    from app.agent.map_data import has_capability
    return raw if has_capability(raw, CAP_STAGE) else None


async def stake(db, debate_id: str, user_id: str, side: str, amount: int) -> DebateStake:
    if side not in ("a", "b"):
        raise DebateError("side must be 'a' or 'b'")
    if not (STAKE_MIN <= amount <= STAKE_MAX):
        raise DebateError(f"amount must be {STAKE_MIN}-{STAKE_MAX}")
    d = await db.get(Debate, debate_id)
    if d is None or d.status != "announced":
        raise DebateError("debate is not open for staking")

    # Pre-check keeps the common double-stake case from charging then refunding;
    # the unique constraint below is the real guard against concurrent inserts.
    existing = (await db.execute(
        select(DebateStake).where(
            DebateStake.debate_id == debate_id, DebateStake.user_id == user_id,
        )
    )).scalar_one_or_none()
    if existing is not None:
        raise DebateError("already staked on this debate")

    from app.services.coin_service import charge, reward
    if not await charge(db, user_id, amount, f"debate_stake:{debate_id}"):
        raise DebateError("Insufficient Soul Coins")

    s = DebateStake(debate_id=debate_id, user_id=user_id, side=side, amount=amount)
    db.add(s)
    try:
        await db.commit()
    except IntegrityError:
        # Lost the race to a concurrent stake by the same user — undo the charge.
        await db.rollback()
        await reward(db, user_id, amount, f"debate_stake_refund:{debate_id}")
        raise DebateError("already staked on this debate")
    await db.refresh(s)

    # Cached pool counter + auto-vote (the stake counts for its side, and blocks
    # a second free vote by the same user).
    if side == "a":
        d.pool_a += amount
        d.votes_a += 1
    else:
        d.pool_b += amount
        d.votes_b += 1
    await db.commit()
    try:
        from app.redis_client import get_redis
        await get_redis().sadd(_VOTERS_KEY.format(id=debate_id), user_id)
    except Exception:
        logger.warning("debate voter dedup set failed", exc_info=True)
    return s


async def vote(db, debate_id: str, user_id: str, side: str) -> None:
    if side not in ("a", "b"):
        raise DebateError("side must be 'a' or 'b'")
    d = await db.get(Debate, debate_id)
    if d is None or d.status != "voting":
        raise DebateError("debate is not open for voting")

    from app.redis_client import get_redis
    added = await get_redis().sadd(_VOTERS_KEY.format(id=debate_id), user_id)
    if not added:
        raise DebateError("already voted on this debate")

    if side == "a":
        d.votes_a += 1
    else:
        d.votes_b += 1
    await db.commit()


# --------------------------------------------------------------------------- #
# Live debate                                                                 #
# --------------------------------------------------------------------------- #
async def run_live(db, debate: Debate) -> Debate:
    """Advance a debate through its live rounds, then open voting.

    On any LLM failure the debate auto-draws and every stake is refunded — a
    dead debate must never keep players' coins.
    """
    if debate.status != "announced":
        raise DebateError("debate is not ready to go live")
    debate.status = "live"
    await db.commit()

    from app.ws.manager import manager
    from app.llm.client import chat
    from app.llm.metering import Meter
    from app.config import settings

    res_a = await _resident(db, debate.resident_a_slug)
    res_b = await _resident(db, debate.resident_b_slug)
    transcript: list[dict] = []
    model = settings.background_model

    try:
        for i in range(ROUNDS):
            side = "a" if i % 2 == 0 else "b"
            speaker = res_a if side == "a" else res_b
            speaker_name = speaker.name if speaker else ("正方" if side == "a" else "反方")
            history = "\n".join(f"{t['speaker']}：{t['text']}" for t in transcript) or "（你先发言）"
            prompt = f"辩题：{debate.topic}\n你是{speaker_name}。\n之前的发言：\n{history}"
            # 必须走 ``app.llm.client.chat()``：它是全仓唯一会加
            # ``thinking={"type": "disabled"}`` 的地方（client.py:148-149）。
            # 直调 ``messages.create`` 时 dashscope 的 deepseek hybrid-thinking
            # 默认开推理，把 max_tokens=200 吃光，响应里没有可用 text block——
            # 生产三场辩论 12 轮辩词全空正是这么来的（llm_usage 里 debate 的
            # output_tokens 全部触顶 201）。同款事故先例：digest_service
            # ``compose_digest`` 的注释。
            text = (await chat(
                DEBATE_SYSTEM,
                [{"role": "user", "content": prompt}],
                model=model,
                max_tokens=200,
                owner="system",
                meter=Meter(scenario="debate"),
            )).strip()[:120]
            if not text:
                # 空辩词不算成功——当 LLM 失败处理，让 except 走 auto-draw
                # 退款，不许把哑剧辩论静默写进 transcript。
                raise RuntimeError(f"debate turn {i + 1} returned empty text")
            turn = {"round": i + 1, "side": side, "speaker": speaker_name, "text": text}
            transcript.append(turn)
            debate.transcript_json = list(transcript)
            await db.commit()
            await manager.broadcast({"type": "debate_turn", "debate_id": debate.id, **turn})
    except Exception:
        logger.warning("debate live aborted (LLM failure); auto-draw refund", exc_info=True)
        await _auto_draw_refund(db, debate)
        await manager.broadcast({"type": "debate_aborted", "debate_id": debate.id})
        return debate

    debate.status = "voting"
    await db.commit()
    # P2 #7:开票的同一刻在场地挂一条 type="script" 的世界事件(见下)。
    await _maybe_open_stage_event(db, debate, res_a, res_b)
    await manager.broadcast({"type": "debate_voting_open", "debate_id": debate.id})
    return debate


async def _maybe_open_stage_event(db, debate: Debate, res_a, res_b) -> None:
    """把这场辩论挂成场地上的一条 ``type="script"`` 世界事件(STAGE_EVENT_ENABLED)。

    **为什么是 "script" 而不是 "news"**:``crowd_service._EVENT_TYPES_WITH_CROWD``
    是 ``("festival", "script")``(crowd_service.py:28),"news" 不在里面 —— 学院
    公开课十五天零到访正是栽在这上面。用 "script" 零改动即获得 ``festival_draw_target``
    的 ×``realism_festival_weight`` 人流拉力(crowd_service.py:207-219),并自动进入
    所有居民的 decide prompt(``get_active_events_cached`` → ``ctx.world_events``)。
    ``WorldEvent.type`` 是 ``String(20)`` 自由文本(models/world_event.py:26),不是
    闭集;``lab/protocol.py:73`` 的那个闭集是 lab 事件总线,与 world_events 表无关。

    **为什么挂在开票这一刻,而不是设计写的「进入 live 之后」**:live 段任何一轮 LLM
    失败都会走 ``_auto_draw_refund`` 并 return(见上),那条路上这场辩论当场 settled
    —— 在进入 live 时建事件就会留下一条指着死辩论、还要拉三倍人流一小时的幽灵事件,
    还得再写一段补偿清理。挂在开票这一刻在同一条成功路径上,墙钟只差六轮 LLM(数十
    秒),而 ``ends_at`` 取 ``debate_vote_window_min`` 本来就该从「投票开始」量起 ——
    这也正是 ``drive_due_debates`` 打 ``_mark_voting_since`` 的同一刻(:337-341)。

    全程 best-effort:世界事件是叙事装饰,建不出来绝不能把一场已经跑完六轮的辩论
    拖进 auto-draw 退款。
    """
    from app.config import settings
    if not settings.stage_event_enabled:
        return
    try:
        venue = await _debate_venue(debate.id)
        if not venue:
            return
        from app.agent.map_data import get_location_by_id
        from app.models.world_event import WorldEvent
        place = (get_location_by_id(venue) or {}).get("name") or "剧院"
        a_name = res_a.name if res_a else "正方"
        b_name = res_b.name if res_b else "反方"
        now = datetime.now(UTC)
        db.add(WorldEvent(
            type="script",
            # WorldEvent.title 是 String(200),Debate.topic 是 String(300)——
            # 真 PG 上不截断就是一条 StringDataRightTruncation。
            title=f"{place}辩论 · {debate.topic}"[:200],
            description=(f"{a_name}与{b_name}正在{place}辩论「{debate.topic}」，"
                         f"居民们可以去{place}旁听、议论。"),
            payload_json={"location_id": venue, "debate_id": debate.id},
            starts_at=now,
            ends_at=now + timedelta(minutes=settings.debate_vote_window_min),
            # 与 script_service.fire_due_scripts(:79-86)同姿势:realism 开时留给
            # event_cron 的 flip 去激活,好让 start 相位的广播与集体记忆照常发生。
            is_active=(False if settings.realism_enabled else True),
        ))
        await db.commit()
    except Exception:
        logger.warning("stage event for debate %s failed", debate.id, exc_info=True)
        # 半截写入不回滚的话,PendingRollbackError 会顺着这条共享 session 传染给
        # drive_due_debates 的下一场辩论(同 event_cron.py:69-77 踩过的坑)。
        try:
            await db.rollback()
        except Exception:
            logger.warning("stage event rollback itself failed", exc_info=True)


async def _auto_draw_refund(db, debate: Debate) -> None:
    from app.services.coin_service import reward
    stakes = (await db.execute(
        select(DebateStake).where(DebateStake.debate_id == debate.id)
    )).scalars().all()
    for s in stakes:
        s.payout = s.amount
    debate.status = "settled"
    debate.winner = "draw"
    debate.settled_at = datetime.now(UTC)
    await db.commit()
    for s in stakes:
        await reward(db, s.user_id, s.amount, f"debate_refund:{debate.id}")


def _aware(ts: datetime | None) -> datetime | None:
    """DB 可能回 naive datetime（sqlite 一定会）——统一补 UTC 再比较。"""
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=UTC)


async def _mark_voting_since(debate_id: str, when: datetime) -> None:
    """记下进入 voting 的时刻。Redis 不可用时静默跳过——有回落推算兜底。"""
    try:
        from app.redis_client import get_redis
        await get_redis().set(_VOTING_SINCE_KEY.format(id=debate_id),
                              when.isoformat(), ex=_VOTING_SINCE_TTL)
    except Exception:
        logger.warning("debate voting-since mark failed for %s", debate_id,
                       exc_info=True)


async def _voting_since(debate_id: str) -> datetime | None:
    """读回进入 voting 的时刻；没有记录或读不出来则返回 None。"""
    try:
        from app.redis_client import get_redis
        raw = await get_redis().get(_VOTING_SINCE_KEY.format(id=debate_id))
    except Exception:
        logger.warning("debate voting-since read failed for %s", debate_id,
                       exc_info=True)
        return None
    if not raw:
        return None
    try:
        return _aware(datetime.fromisoformat(raw))
    except ValueError:
        logger.warning("debate voting-since unparseable for %s: %r",
                       debate_id, raw)
        return None


async def _clear_voting_since(debate_id: str) -> None:
    """辩论进入终态后清掉标记，不留垃圾键。"""
    try:
        from app.redis_client import get_redis
        await get_redis().delete(_VOTING_SINCE_KEY.format(id=debate_id))
    except Exception:
        logger.debug("debate voting-since clear failed for %s", debate_id,
                     exc_info=True)


async def drive_due_debates(db) -> dict:
    """把到期的辩论推过 announced → live → voting → settled。

    E3/#3-1：``run_live`` 与 ``settle`` 在 app/ 下本来零调用方，辩论建出来就
    停在 announced，而 stake 接口是开放的且真扣币 —— 玩家的押注被永久冻结。

    两个阶段的判据来源不同，这是有意的：

    - ``announced → live`` 用 ``starts_at``：押注窗口本来就从辩论公布开始算。
    - ``voting → settled`` 用**进入 voting 的真实时刻**（``run_live`` 成功时
      记进 Redis 的 ``_VOTING_SINCE_KEY``），而不是 ``starts_at`` 推算。判据
      写成 ``starts_at + stake + vote`` 会让一场积压的辩论在同一轮里被
      ``run_live`` 之后立刻 ``settle``：两者之间零耗时，票数全为初值，
      ``settle`` 走平局分支，玩家一票都投不上。而这恰恰是驱动器首次跑积压时
      的必然状态。Redis 标记缺失（重启，或本驱动器上线前就已在 voting 的历史
      数据）时回落到 ``starts_at + stake_window``，退化成保守结算而不是卡死。

    **超期兜底先跑**：卡在任何非终态超过 ``debate_stuck_hours`` 的一律平局
    全额退款，且优先于 run_live —— 否则一场卡了两天的辩论会先被拉起来跑一轮
    LLM，钱还是玩家的、时间却是错的。

    每条辩论单独 try/except：一场炸了不能让整轮 cron 停摆。
    """
    from app.config import settings

    now = datetime.now(UTC)
    moved = {"live": 0, "settled": 0, "refunded": 0}

    stuck_before = now - timedelta(hours=settings.debate_stuck_hours)
    rows = (await db.execute(
        select(Debate).where(Debate.status.in_(("announced", "live", "voting")))
    )).scalars().all()

    handled: set[str] = set()
    for d in rows:
        started = _aware(d.starts_at)
        if started is None or started > stuck_before:
            continue
        try:
            await _auto_draw_refund(db, d)
            # 本轮已终结（平局退款），后两个循环都不该再碰——与 loop 2 的
            # finally 写的是同一个 handled 集合，不是两套独立的登记逻辑。
            handled.add(d.id)
            moved["refunded"] += 1
            logger.warning("debate %s stuck in %s for over %dh — auto-draw refunded",
                           d.id, d.status, settings.debate_stuck_hours)
            await _clear_voting_since(d.id)
        except Exception:
            logger.warning("debate stuck-sweep failed for %s", d.id, exc_info=True)

    live_before = now - timedelta(minutes=settings.debate_stake_window_min)
    for d in rows:
        if d.id in handled or d.status != "announced":
            continue
        started = _aware(d.starts_at)
        if started is None or started > live_before:
            continue
        try:
            await run_live(db, d)
            moved["live"] += 1
            if d.status == "voting":
                # run_live 内部 LLM 失败会走 _auto_draw_refund 直接置 settled，
                # 那种情况没有投票窗口可言，不记标记。
                await _mark_voting_since(d.id, now)
        except Exception:
            logger.warning("debate run_live failed for %s", d.id, exc_info=True)
        finally:
            # 无论成功、auto-draw 还是抛异常，本轮都不再让它进入 settle 循环。
            handled.add(d.id)

    for d in rows:
        if d.id in handled or d.status != "voting":
            continue
        voting_since = await _voting_since(d.id)
        if voting_since is None:
            # 回落：没有标记（Redis 重启，或本驱动器上线前就已在 voting 的
            # 历史数据）→ 用 starts_at 推算，与本次修复前的行为一致。
            started = _aware(d.starts_at)
            if started is None:
                continue
            voting_since = started + timedelta(
                minutes=settings.debate_stake_window_min)
        if (now - voting_since).total_seconds() < settings.debate_vote_window_min * 60:
            continue
        try:
            await settle(db, d.id)
            moved["settled"] += 1
            await _clear_voting_since(d.id)
        except Exception:
            logger.warning("debate settle failed for %s", d.id, exc_info=True)

    return moved


# --------------------------------------------------------------------------- #
# Settlement                                                                  #
# --------------------------------------------------------------------------- #
async def settle(db, debate_id: str) -> dict:
    """Settle a debate in the voting stage. Idempotent: a settled debate is a no-op."""
    d = await db.get(Debate, debate_id)
    if d is None:
        raise DebateError("no such debate")
    if d.status == "settled":
        return {"winner": d.winner, "already": True}
    if d.status != "voting":
        raise DebateError("debate is not ready to settle")

    stakes = (await db.execute(
        select(DebateStake).where(DebateStake.debate_id == debate_id)
    )).scalars().all()

    if d.votes_a == d.votes_b:
        return await _finish_draw(db, d, stakes)

    winner = "a" if d.votes_a > d.votes_b else "b"
    loser = "b" if winner == "a" else "a"
    winner_pool = sum(s.amount for s in stakes if s.side == winner)
    loser_pool = sum(s.amount for s in stakes if s.side == loser)
    distributable = int(loser_pool * (1 - BURN_RATE))  # 5% burned
    burn = loser_pool - distributable

    from app.services.coin_service import reward
    total_paid = 0
    for s in stakes:
        if s.side == winner:
            bonus = int(distributable * s.amount / winner_pool) if winner_pool else 0
            s.payout = s.amount + bonus
        else:
            s.payout = 0
    d.status = "settled"
    d.winner = winner
    d.settled_at = datetime.now(UTC)
    await db.commit()

    for s in stakes:
        if s.payout:
            await reward(db, s.user_id, s.payout, f"debate_win:{debate_id}")
            total_paid += s.payout

    await _resident_aftermath(db, d, winner)
    return {"winner": winner, "winner_pool": winner_pool, "loser_pool": loser_pool,
            "burn": burn, "distributable": distributable, "total_paid": total_paid}


async def _finish_draw(db, d: Debate, stakes) -> dict:
    from app.services.coin_service import reward
    for s in stakes:
        s.payout = s.amount
    d.status = "settled"
    d.winner = "draw"
    d.settled_at = datetime.now(UTC)
    await db.commit()
    for s in stakes:
        await reward(db, s.user_id, s.amount, f"debate_refund:{d.id}")
    return {"winner": "draw", "refunded": sum(s.amount for s in stakes)}


async def _resident_aftermath(db, d: Debate, winner: str) -> None:
    """Winner's mood lifts (E1); both residents remember the debate."""
    try:
        from app.services.mood_service import apply_mood_event_by_id
        from app.memory.service import MemoryService
        win_res = await _resident(db, d.resident_a_slug if winner == "a" else d.resident_b_slug)
        lose_res = await _resident(db, d.resident_b_slug if winner == "a" else d.resident_a_slug)
        mem = MemoryService(db)
        if win_res:
            await apply_mood_event_by_id(db, win_res.id, 0.3, 0.1)
            await mem.add_memory(win_res.id, "event", f"我在辩论「{d.topic}」中赢了，观众站在我这边。",
                                 importance=0.7, source="debate")
        if lose_res:
            await mem.add_memory(lose_res.id, "event", f"辩论「{d.topic}」我输了，得再想想我的观点。",
                                 importance=0.6, source="debate")
    except Exception:
        logger.warning("debate aftermath failed", exc_info=True)
    # S1-3 (opportunistic seam): winner stance reinforced toward its pole,
    # loser regresses toward 0 — only ever fires when settle is actually
    # driven, which app code does not do today. Best-effort + gated.
    try:
        from app.config import settings
        if settings.polis_opinion_enabled:
            from app.services.opinion_service import OpinionService
            await OpinionService(db).update_from_debate(d, seed_only=False)
    except Exception:
        logger.warning("opinion update from settle failed", exc_info=True)
    # P2 #10:在场观众的收益。全部落在「记忆/心情/社交需求/关系」四个**非货币**
    # 层 —— settle 已经有一条真金链路(stake 时 charge 已扣走玩家的币 :99,settle
    # 只做重分配:distributable=int(loser_pool*0.95)、burn=loser_pool-distributable
    # :396-414),出账恒 ≤ 入账、净销毁 burn+取整余数,是净 sink 不是铸币口。给观众
    # 发 SC 就是开第二条铸币口且无对应 sink,并与 settle 分账双花。这里一枚不动。
    #
    # 跑在 settle 的 await db.commit()(:405)之后,辩论早已 settled;异常自吞并
    # rollback,既不回染 M7 的生命周期护栏,也不把中断的事务留给上面的
    # opinion_service(照 execute/basic.py:99-103 _charge_meal 的形状)。
    try:
        from app.config import settings
        if settings.stage_event_enabled:
            await _audience_aftermath(db, d, winner)
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        logger.warning("debate audience aftermath failed", exc_info=True)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
async def _resident(db, slug: str) -> Resident | None:
    return (await db.execute(select(Resident).where(Resident.slug == slug))).scalar_one_or_none()


def _stable_audience_rank(seed: str, resident_id: str) -> bytes:
    """稳定排序键(照 crowd_service._stable_rank 的形状)。

    按 id 直接排序会让同几个人永远占满 8 个名额;按 seed(= debate id)加盐后,
    每场辩论的截断名单不同,但同一场重跑恒等 —— settle 幂等要求它是纯函数。
    """
    return hashlib.sha256(f"{seed}\x1f{resident_id}".encode("utf-8")).digest()


async def stage_venue_of(db, debate_id: str) -> str | None:
    """这场辩论的剧院地点 id;没有场地信息则 None(= 今天每一场辩论的形态)。

    Debate 模型没有 location 列,本批次不动 schema(红线:迁移与行为变更不得同一次
    变更)。地点走已有的 WorldEvent 通道,payload 契约由 design_P2.md §②-a 定:
    ``type="script"`` + ``payload_json={"location_id": venue, "debate_id": id}``。

    payload 的过滤放在 Python 侧而不是 SQL:world_events.payload_json 是
    ``sa.JSON()``(PG 上是 json 不是 jsonb),而测试库是 sqlite(JSON 运算符不可用)。
    drive_due_debates 的时间判据同样是 Python 侧过滤,口径一致。

    location_id 的读取经 resolve_event_location_id,与 crowd_service.
    active_event_location 同一个解析器 —— 否则「人流拉去哪」与「观众算在哪」会分叉。
    """
    from app.models.world_event import WorldEvent
    from app.services.event_location import resolve_event_location_id

    rows = (await db.execute(
        select(WorldEvent)
        .where(WorldEvent.type == "script")
        .order_by(WorldEvent.created_at.desc())
        .limit(_VENUE_SCAN_LIMIT)
    )).scalars().all()
    for ev in rows:
        payload = ev.payload_json or {}
        if payload.get("debate_id") != debate_id:
            continue
        loc = resolve_event_location_id(payload)
        return loc if isinstance(loc, str) and loc else None
    return None


async def stage_audience(
    db, venue: str, *, seed: str, exclude_slugs: tuple[str, ...] = (),
) -> list[Resident]:
    """此刻站在 venue 里的清醒自治 sim 居民,至多 AUDIENCE_LIMIT 人。

    「在不在场」用 map_data.capability_location_at 而**不是** get_location_id_at:
    后者首命中即返,命中序 = dict 插入序 = 静态在前、动态追加在尾,而
    theater(172,40,178,50) 完全落在 outdoor 街区 east_gardens(140,35,179,58) 内部
    —— 生产实测 get_location_id_at(175,45) 返 "east_gardens"。照粗查写,观众名单恒
    为空、观众收益静默失效且零告警。

    存量 dynamic_locations 行没有 capabilities 键 → 归一成空 dict → 恒返空名单 →
    调用方走老行为。缺省安全,回填前后都不炸。

    候选集与 crowd_service.market_day_crowd_cohort 同源:autonomous +
    resident_type ∈ {npc, resident}。UGC 角色(resident_type="character")不进名单
    —— 观众收益写的是 needs/关系,不该落到非 sim 身份上。只有 sleeping 被排除:
    cohort 那边还挡 chatting/socializing 是因为它要把人**拉走**,而在场观众正在场
    内说话恰恰是看戏的常态。
    """
    from app.agent.location_caps import CAP_STAGE
    from app.agent.map_data import capability_location_at

    rows = (await db.execute(
        select(Resident).where(
            Resident.is_autonomous,
            Resident.resident_type.in_(["npc", "resident"]),
            Resident.status.not_in(["sleeping"]),
        )
    )).scalars().all()
    present = [
        r for r in rows
        if r.slug not in exclude_slugs
        and capability_location_at(r.tile_x or 0, r.tile_y or 0, CAP_STAGE) == venue
    ]
    present.sort(key=lambda r: _stable_audience_rank(seed, str(r.id)))
    return present[:AUDIENCE_LIMIT]


async def _audience_aftermath(db, d: Debate, winner: str) -> None:
    """在场观众的非货币收益:记忆 / 心情 / 社交需求 / 关系。**零 SC 流动**。

    social 是这四层里最该给的一项:needs.social 恢复会改变 most_critical
    (needs.py:65)与 _crowd_hint(decide/basic.py:410),直接治动机侧 —— 看了一场热闹
    的辩论就该不那么孤独。needs 写入额外挂 realism_enabled:needs 体系本来就归
    realism,闸关的世界不该凭空多出 meta_json["needs"]。

    write_needs 不 commit(needs.py:29-34)且必须整体重赋 meta_json 才触发
    SQLAlchemy 脏检测 —— 所以整批写完统一 commit 一次。
    """
    venue = await stage_venue_of(db, d.id)
    if not venue:
        return
    audience = await stage_audience(
        db, venue, seed=d.id,
        exclude_slugs=(d.resident_a_slug, d.resident_b_slug),
    )
    if not audience:
        return

    from app.agent.map_data import get_location_by_id
    from app.config import settings
    from app.memory.service import MemoryService
    from app.services.mood_service import apply_mood_event
    from app.services.relation_service import bump

    venue_name = (get_location_by_id(venue) or {}).get("name") or venue
    win_slug = d.resident_a_slug if winner == "a" else d.resident_b_slug
    win_res = await _resident(db, win_slug)
    win_name = win_res.name if win_res else ("正方" if winner == "a" else "反方")

    mem = MemoryService(db)
    for r in audience:
        await mem.add_memory(
            r.id, "event",
            f"我在{venue_name}看完了辩论「{d.topic}」,{win_name}赢了。",
            importance=AUDIENCE_MEMORY_IMPORTANCE, source="debate")
        # 已经拿到 ORM 对象,用 apply_mood_event 而不是 ..._by_id,省一次 db.get。
        await apply_mood_event(db, r, AUDIENCE_MOOD_VALENCE, AUDIENCE_MOOD_AROUSAL)

    if settings.realism_enabled:
        from app.agent.needs import get_needs, write_needs
        for r in audience:
            needs = get_needs(r)
            needs["social"] = min(1.0, needs["social"] + AUDIENCE_SOCIAL_RESTORE)
            write_needs(r, needs)
        await db.commit()

    # 同场观众两两加熟。O(n²) 但 n ≤ AUDIENCE_LIMIT(8 → 28 次),且每场辩论只在
    # settle 时跑一次,不在 tick 热路径上。
    for i, r1 in enumerate(audience):
        for r2 in audience[i + 1:]:
            await bump(db, r1.id, r2.id, AUDIENCE_FAMILIARITY, 0.0)
