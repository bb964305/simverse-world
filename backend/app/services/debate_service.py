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
from datetime import datetime, UTC

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
    from app.llm.client import get_client
    from app.llm.metering import record_usage
    from app.config import settings

    res_a = await _resident(db, debate.resident_a_slug)
    res_b = await _resident(db, debate.resident_b_slug)
    transcript: list[dict] = []
    client = get_client("system")
    model = settings.background_model

    try:
        for i in range(ROUNDS):
            side = "a" if i % 2 == 0 else "b"
            speaker = res_a if side == "a" else res_b
            speaker_name = speaker.name if speaker else ("正方" if side == "a" else "反方")
            history = "\n".join(f"{t['speaker']}：{t['text']}" for t in transcript) or "（你先发言）"
            prompt = f"辩题：{debate.topic}\n你是{speaker_name}。\n之前的发言：\n{history}"
            resp = await client.messages.create(
                model=model, max_tokens=200, system=DEBATE_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            await record_usage("debate", model=model, owner="system", response=resp)
            text = _extract_text(resp).strip()[:120]
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


def _extract_text(resp) -> str:
    for block in resp.content:
        if hasattr(block, "text"):
            return block.text
    return ""
