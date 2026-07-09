"""WS progress push for forge pipelines (P1-5).

Replaces the frontend's setInterval polling: each pipeline stage transition and
the terminal done/error state is pushed to the creating user over the existing
WS channel. All sends are best-effort — a WS hiccup must never break a forge run,
so every push is wrapped and swallowed. Runs in the API process (deep pipeline
background task and the guided/quick background coroutines), so `manager.send`
reaches the user directly or via Redis pub/sub across workers.
"""

import logging

from app.ws.manager import manager

logger = logging.getLogger(__name__)


async def notify_forge_progress(user_id: str, forge_id: str, stage: str, status: str) -> None:
    """Push one stage transition. `stage`/`status` are advisory; the client
    fetches canonical state on receipt."""
    try:
        await manager.send(user_id, {
            "type": "forge_progress",
            "forge_id": forge_id,
            "stage": stage,
            "status": status,
        })
    except Exception:
        logger.warning("forge_progress push failed for %s", forge_id, exc_info=True)


async def notify_forge_done(user_id: str, forge_id: str) -> None:
    try:
        await manager.send(user_id, {
            "type": "forge_done",
            "forge_id": forge_id,
            "status": "done",
        })
    except Exception:
        logger.warning("forge_done push failed for %s", forge_id, exc_info=True)


async def notify_forge_error(user_id: str, forge_id: str, error: str) -> None:
    try:
        await manager.send(user_id, {
            "type": "forge_error",
            "forge_id": forge_id,
            "status": "error",
            "error": error,
        })
    except Exception:
        logger.warning("forge_error push failed for %s", forge_id, exc_info=True)
