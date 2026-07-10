"""E12 season scoring + leaderboard (+ C3 season helpers).

Points accumulate on the S2 bus (season_scorer). A per-user daily cap (100, via
Redis) prevents grinding. Leaderboard supports around_me. Settlement snapshots
the final ranks idempotently.
"""

import time
import logging
from datetime import datetime, UTC

from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from app.database import async_session
from app.models.season import Season, SeasonScore
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

DAILY_CAP = 100
_active_cache: dict = {"ts": 0.0, "season_id": None}
_ACTIVE_TTL = 60.0


async def get_active_season(db) -> Season | None:
    return (await db.execute(
        select(Season).where(Season.status == "active").order_by(Season.starts_at.desc())
    )).scalars().first()


async def _active_season_id(db) -> str | None:
    now = time.monotonic()
    if now - _active_cache["ts"] < _ACTIVE_TTL and _active_cache["ts"] > 0:
        return _active_cache["season_id"]
    season = await get_active_season(db)
    _active_cache["ts"] = now
    _active_cache["season_id"] = season.id if season else None
    return _active_cache["season_id"]


def _invalidate_active():
    _active_cache["ts"] = 0.0


async def add_points(user_id: str, points: int, category: str) -> int:
    """Add points to the active season for a user, respecting the daily cap. Returns added."""
    if points <= 0:
        return 0
    async with async_session() as db:
        season_id = await _active_season_id(db)
        if not season_id:
            return 0
        r = get_redis()
        key = f"season:{season_id}:{user_id}:{datetime.now(UTC).date().isoformat()}"
        current = int(await r.get(key) or 0)
        if current >= DAILY_CAP:
            return 0
        added = min(points, DAILY_CAP - current)
        await r.incrby(key, added)
        await r.expire(key, 2 * 86400)

        score = (await db.execute(
            select(SeasonScore).where(SeasonScore.season_id == season_id, SeasonScore.user_id == user_id)
        )).scalar_one_or_none()
        if score is None:
            db.add(SeasonScore(season_id=season_id, user_id=user_id, points=added,
                               breakdown_json={category: added}, updated_at=datetime.now(UTC)))
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                score = (await db.execute(
                    select(SeasonScore).where(SeasonScore.season_id == season_id, SeasonScore.user_id == user_id)
                )).scalar_one()
        if score is not None:
            score.points += added
            bd = dict(score.breakdown_json or {})
            bd[category] = bd.get(category, 0) + added
            score.breakdown_json = bd
            score.updated_at = datetime.now(UTC)
            await db.commit()
        return added


async def leaderboard(db, season_id: str, user_id: str | None = None, around_me: bool = False) -> dict:
    top = (await db.execute(
        select(SeasonScore).where(SeasonScore.season_id == season_id)
        .order_by(SeasonScore.points.desc(), SeasonScore.user_id).limit(50)
    )).scalars().all()

    def _row(s, rank):
        return {"rank": rank, "user_id": s.user_id, "points": s.points, "breakdown": s.breakdown_json or {}}

    result = {"top": [_row(s, i + 1) for i, s in enumerate(top)]}

    if around_me and user_id:
        all_scores = (await db.execute(
            select(SeasonScore.user_id, SeasonScore.points).where(SeasonScore.season_id == season_id)
            .order_by(SeasonScore.points.desc(), SeasonScore.user_id)
        )).all()
        ranks = {uid: i + 1 for i, (uid, _) in enumerate(all_scores)}
        my_rank = ranks.get(user_id)
        if my_rank:
            lo, hi = my_rank - 2, my_rank + 2
            around = [{"rank": i + 1, "user_id": uid, "points": pts}
                      for i, (uid, pts) in enumerate(all_scores) if lo <= i + 1 <= hi]
            result["around_me"] = {"my_rank": my_rank, "rows": around}

    # Attach display names so the UI never has to show raw user ids.
    from app.models.user import User
    ids = {r["user_id"] for r in result["top"]}
    ids.update(r["user_id"] for r in result.get("around_me", {}).get("rows", []))
    if ids:
        names = dict((await db.execute(
            select(User.id, User.name).where(User.id.in_(ids))
        )).all())
        for r in result["top"]:
            r["name"] = names.get(r["user_id"], "")
        for r in result.get("around_me", {}).get("rows", []):
            r["name"] = names.get(r["user_id"], "")
    return result


async def settle_season(db, season: Season) -> dict:
    """Snapshot final ranks + settle (idempotent via payload_json.settled)."""
    payload = dict(season.payload_json or {})
    if payload.get("settled"):
        return payload
    scores = (await db.execute(
        select(SeasonScore).where(SeasonScore.season_id == season.id)
        .order_by(SeasonScore.points.desc(), SeasonScore.user_id)
    )).scalars().all()
    final_ranks = [{"rank": i + 1, "user_id": s.user_id, "points": s.points} for i, s in enumerate(scores)]
    payload["final_ranks"] = final_ranks
    payload["settled"] = True
    season.payload_json = payload
    season.status = "settled"
    await db.commit()

    # Top-3 SC bonus (achievement badge registration deferred).
    from app.services.coin_service import reward
    for entry in final_ranks[:3]:
        bonus = [200, 120, 80][entry["rank"] - 1]
        try:
            await reward(db, entry["user_id"], bonus, f"season_rank:{season.id}")
        except Exception:
            logger.warning("season bonus failed for %s", entry["user_id"], exc_info=True)
    return payload
