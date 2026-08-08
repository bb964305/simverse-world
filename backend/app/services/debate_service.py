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

_VOTERS_KEY = "sv:debate_voters:{id}"

#: run_live 把辩论推进到 voting 的真实时刻。debates 表没有相位时间列，而本
#: 批次不动 schema（红线：迁移与行为变更不得同一次变更）。settle 的判据必须
#: 是「投票开了多久」而不是「辩论建了多久」——否则一场积压的 announced 辩论
#: 会在同一轮里被 run_live 之后立刻 settle，玩家一票都投不上，结果恒为平局。
#: Redis 丢失（重启 / 本驱动器上线前就已在 voting 的历史数据）时回落到
#: starts_at 推算，退化成保守结算而不是卡死。
_VOTING_SINCE_KEY = "sv:debate_voting_since:{id}"
_VOTING_SINCE_TTL = 7 * 86400

DEBATE_SYSTEM = (
    "你正在参加一场辩论擂台。你要为自己的立场辩护，语气鲜明、有理有据，"
    "针对上一位发言者的观点回应。60 字以内，只输出你的发言。"
)


class DebateError(Exception):
    """Invalid debate request (router maps to 400)."""


# --------------------------------------------------------------------------- #
# Setup / staking                                                             #
# --------------------------------------------------------------------------- #
async def create_debate(db, topic: str, a_slug: str, b_slug: str) -> Debate:
    d = Debate(topic=topic, resident_a_slug=a_slug, resident_b_slug=b_slug, status="announced")
    db.add(d)
    await db.commit()
    await db.refresh(d)
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
    await manager.broadcast({"type": "debate_voting_open", "debate_id": debate.id})
    return debate


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


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
async def _resident(db, slug: str) -> Resident | None:
    return (await db.execute(select(Resident).where(Resident.slug == slug))).scalar_one_or_none()
