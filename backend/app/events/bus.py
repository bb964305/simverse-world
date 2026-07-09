"""In-process synchronous domain event bus (S2).

Tiny publish/subscribe used to decouple side-effects (achievements, digests,
feeds) from the code paths that produce domain events. Handlers are awaited one
by one; a single handler failing is logged and skipped so one bad handler can't
break the emitter or the other handlers.

Usage:
    from app.events.bus import on, emit

    @on("chat_completed")
    async def _award(db, user_id, resident_id, turns, **kw): ...

    await emit(db, "chat_completed", user_id=uid, resident_id=rid, turns=n)
"""

import logging
from collections import defaultdict
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

Handler = Callable[..., Awaitable[None]]

_handlers: dict[str, list[Handler]] = defaultdict(list)


def on(event: str) -> Callable[[Handler], Handler]:
    """Decorator: register an async handler for an event name."""
    def decorator(fn: Handler) -> Handler:
        _handlers[event].append(fn)
        return fn
    return decorator


async def emit(db, event: str, **kw) -> None:
    """Fire all handlers for an event. Handler failures are isolated."""
    for handler in _handlers.get(event, []):
        try:
            await handler(db, **kw)
        except Exception:
            logger.warning("event handler %s for %r failed", getattr(handler, "__name__", handler), event, exc_info=True)


def _reset_for_tests() -> None:  # pragma: no cover - test helper
    _handlers.clear()
