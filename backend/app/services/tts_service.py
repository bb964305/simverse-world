"""E5 resident voice (TTS): SBTI→voice mapping, per-day quota, file cache.

Cost control: cache first (repeated lines like greetings hit often), a per-user
daily free quota via Redis, and only synthesize on a miss (shared httpx client).
"""

import hashlib
import logging
from pathlib import Path
from datetime import datetime, UTC

from app.config import settings
from app.http import get_client
from app.redis_client import get_redis

logger = logging.getLogger(__name__)

MAX_TEXT = 300
TTS_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "tts"

VOICE_PRESETS = ["calm_male", "bright_female", "warm_male", "cool_female", "gentle", "energetic"]


class TTSError(Exception):
    """Raised for bad TTS requests (router maps to 400)."""


class TTSQuotaError(Exception):
    """Raised when the daily quota is exhausted (router maps to 429)."""


def voice_for(resident) -> str:
    """Derive a stable voice id from SBTI (meta_json.voice overrides)."""
    meta = resident.meta_json or {}
    if meta.get("voice"):
        return str(meta["voice"])
    sbti = meta.get("sbti", {}).get("type", "") or "default"
    idx = sum(ord(c) for c in sbti) % len(VOICE_PRESETS)
    speed = 90 + (sum(ord(c) for c in sbti) % 21)  # 90..110
    return f"{VOICE_PRESETS[idx]}:{speed}"


async def _check_quota(user_id: str) -> None:
    r = get_redis()
    key = f"tts:{user_id}:{datetime.now(UTC).date().isoformat()}"
    count = await r.incr(key)
    if count == 1:
        await r.expire(key, 2 * 86400)
    if count > settings.tts_daily_free_quota:
        raise TTSQuotaError("Daily TTS quota exhausted")


async def synthesize(user_id: str, resident, text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise TTSError("text is required")
    if len(text) > MAX_TEXT:
        raise TTSError(f"text too long (max {MAX_TEXT})")

    voice = voice_for(resident)
    h = hashlib.sha256(f"{voice}|{text}".encode("utf-8")).hexdigest()
    path = TTS_DIR / f"{h}.mp3"
    url = f"/static/tts/{h}.mp3"
    duration = round(len(text) * 0.18, 2)

    # Cache hit is free (no quota, no synthesis).
    if path.exists():
        return {"url": url, "duration": duration, "cached": True, "voice": voice}

    # Miss → count against the daily quota, then synthesize.
    await _check_quota(user_id)
    if not settings.tts_base_url or not settings.tts_api_key:
        raise TTSError("TTS not configured")

    resp = await get_client().post(
        f"{settings.tts_base_url}/audio/speech",
        headers={"Authorization": f"Bearer {settings.tts_api_key}"},
        json={"model": settings.tts_model, "voice": voice.split(":")[0], "input": text},
    )
    resp.raise_for_status()
    TTS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(resp.content)
    return {"url": url, "duration": duration, "cached": False, "voice": voice}
