"""Shop effect registry (S3).

Purchase effects are dispatched by item ``kind``. Feature slices (D2 consumables,
B3 decor, A3 gifts) register their own handlers here; S3 ships the registry and
pipeline with no built-in effects (a kind with no handler is a valid no-op — the
charge + Purchase row still happen).

A handler signature is: async def handler(db, user_id, item, qty, context) -> dict | None
The optional returned dict is surfaced to the client as `effect`.
"""

import logging
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

EffectHandler = Callable[..., Awaitable[dict | None]]

_effects: dict[str, EffectHandler] = {}


def register(kind: str) -> Callable[[EffectHandler], EffectHandler]:
    """Decorator: register an effect handler for an item kind."""
    def decorator(fn: EffectHandler) -> EffectHandler:
        _effects[kind] = fn
        return fn
    return decorator


async def apply_effect(db, user_id: str, item, qty: int, context: dict | None) -> dict | None:
    """Run the effect handler for the item's kind, if any. Isolated from purchase."""
    handler = _effects.get(item.kind)
    if handler is None:
        return None
    try:
        return await handler(db, user_id, item, qty, context or {})
    except Exception:
        logger.warning("shop effect for kind=%s (item=%s) failed", item.kind, item.code, exc_info=True)
        return None


def _reset_for_tests() -> None:  # pragma: no cover - test helper
    _effects.clear()
