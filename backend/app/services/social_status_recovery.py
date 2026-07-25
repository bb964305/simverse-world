"""DB-side recycling of stuck ``socializing`` resident status (R4).

Why this exists
---------------
NPC<->NPC conversation locks both parties by writing
``Resident.status = "socializing"`` (``app/agent/chat.py``) and only releases
them in the ``finally`` block at the end of the conversation. A worker killed
mid-conversation (OOM, ``kill -9``, container restart, deploy) never reaches
that ``finally``: the row stays "socializing" **forever**, and every later
attempt to talk to that resident is dropped by the ``target.status in
("chatting", "socializing", "sleeping")`` pre-check — resident socialising goes
permanently silent with no error anywhere.

The Redis locks in ``app/ws/manager.py`` already carry a TTL and self-heal, but
``lock_socializing`` has no caller outside that module: the NPC<->NPC path is
guarded by the DB status alone. So recovery here is **timestamp driven** — the
lock write now stamps ``meta_json["social_lock"]["since"]`` and this sweep
returns anything older than the threshold to ``idle``. A still-held Redis
social lock (should that path ever be wired up) is honoured as a "leave it
alone" cross-check.

meta_json is used rather than a new column on purpose: ``residents`` has no
``updated_at``, and adding a column would mean a migration in a batch where two
other parallel lines already own the next revision numbers.

Configuration is plain **environment variables**, deliberately NOT
``app.config.Settings`` fields (batch red line: config.py untouched while two
other lines are editing its tail; the keys get registered in .env.example at
integration time):

    SOCIAL_STATUS_RECOVERY_ENABLED  default "true"  — one-switch off
    SOCIAL_STATUS_STALE_SECONDS     default 600     — = ws.manager.SOCIAL_LOCK_TTL
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, UTC

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.models.resident import Resident
from app.ws.manager import SOCIALIZING_PREFIX, SOCIAL_LOCK_TTL

logger = logging.getLogger(__name__)

SOCIALIZING = "socializing"
_META_KEY = "social_lock"
_TRUE = {"1", "true", "yes", "on"}


# --------------------------------------------------------------------------- #
# switches                                                                     #
# --------------------------------------------------------------------------- #

def _settings_default(name: str, default):
    """Registered default for an env knob (收口 2026-07-25B): the env var stays
    the runtime source, ``Settings`` owns the fallback so every .env.example key
    maps to a real field."""
    try:
        from app.config import settings

        return getattr(settings, name.lower(), default)
    except Exception:  # config import must never break the recovery pass
        return default


def recovery_enabled() -> bool:
    raw = os.environ.get("SOCIAL_STATUS_RECOVERY_ENABLED")
    if raw is None or raw.strip() == "":
        return bool(_settings_default("SOCIAL_STATUS_RECOVERY_ENABLED", True))
    return raw.strip().lower() in _TRUE


def stale_threshold_s() -> float:
    """Age at which a socializing stamp is considered orphaned.

    Aligned with the Redis social lock TTL by default: a conversation that
    outlives the lock its Redis counterpart would have held is, by the system's
    own definition, no longer alive.
    """
    fallback = float(_settings_default(
        "SOCIAL_STATUS_STALE_SECONDS", SOCIAL_LOCK_TTL) or SOCIAL_LOCK_TTL)
    raw = os.environ.get("SOCIAL_STATUS_STALE_SECONDS")
    if raw is None or raw.strip() == "":
        return fallback
    try:
        value = float(raw)
    except ValueError:
        logger.warning("invalid SOCIAL_STATUS_STALE_SECONDS=%r — using default", raw)
        return fallback
    return value if value > 0 else fallback


# --------------------------------------------------------------------------- #
# stamping (called from the chat engine)                                       #
# --------------------------------------------------------------------------- #

def mark_socializing(
    resident: Resident, *, partner_id: str | None = None, now: datetime | None = None
) -> None:
    """Take the DB-side social lock **with** a timestamp.

    Caller still owns the commit (the chat engine locks both parties in one
    transaction).
    """
    meta = dict(resident.meta_json or {})
    meta[_META_KEY] = {
        "since": (now or datetime.now(UTC)).isoformat(),
        "partner": partner_id,
    }
    resident.status = SOCIALIZING
    resident.meta_json = meta
    flag_modified(resident, "meta_json")


def clear_socializing(resident: Resident, *, status: str = "idle") -> None:
    """Release the DB-side social lock and drop the stamp."""
    resident.status = status
    meta = dict(resident.meta_json or {})
    if _META_KEY in meta:
        meta.pop(_META_KEY)
        resident.meta_json = meta
        flag_modified(resident, "meta_json")


def socializing_since(resident: Resident) -> datetime | None:
    """Parsed lock timestamp, or None when absent/unparseable (= treat as old)."""
    entry = (getattr(resident, "meta_json", None) or {}).get(_META_KEY)
    if not isinstance(entry, dict):
        return None
    raw = entry.get("since")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# the sweep                                                                    #
# --------------------------------------------------------------------------- #

async def _redis_lock_held(resident_id: str) -> bool:
    """True when ws.manager still holds a (TTL'd) social lock for this resident.

    Fail-open: an unreachable Redis must not block recovery, so errors answer
    "not held" and the timestamp verdict stands.
    """
    try:
        from app.redis_client import get_redis

        return bool(await get_redis().exists(SOCIALIZING_PREFIX + resident_id))
    except Exception:
        logger.debug("social lock probe failed for %s", resident_id, exc_info=True)
        return False


async def recover_stale_socializing(db, *, now: datetime | None = None) -> int:
    """Return orphaned ``socializing`` residents to ``idle``. Returns the count.

    Stale = stamp older than :func:`stale_threshold_s`, **or** no stamp at all
    (rows stuck by the pre-fix code, which never wrote one). Only
    ``socializing`` is touched: player chat ("chatting") and "sleeping" have
    their own lifecycles and are out of scope here.
    """
    if not recovery_enabled():
        return 0
    now = now or datetime.now(UTC)
    threshold = stale_threshold_s()

    rows = (await db.execute(
        select(Resident).where(Resident.status == SOCIALIZING)
    )).scalars().all()

    recovered = 0
    for resident in rows:
        since = socializing_since(resident)
        if since is not None and (now - since).total_seconds() < threshold:
            continue
        if await _redis_lock_held(resident.id):
            continue
        logger.warning(
            "recycling stale socializing lock on %s (since=%s, threshold=%.0fs) — "
            "a worker died without releasing it (R4)",
            resident.slug, since.isoformat() if since else "unstamped", threshold,
        )
        clear_socializing(resident)
        recovered += 1

    if recovered:
        await db.commit()
    return recovered
