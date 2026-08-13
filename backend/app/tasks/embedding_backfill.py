"""Compensation task for memory embeddings (P0-5).

Legacy code wrote zero-vectors on Ollama failures, which poison
cosine-distance retrieval (distance to a zero vector is NaN). This task:
1. Nulls out any remaining all-zero embeddings (one-off data cleanup)
2. Periodically recomputes embeddings for event memories left at NULL
"""
import asyncio
import logging
import time

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.memory.embedding import generate_embeddings_batch
from app.models.memory import Memory
from app.tasks.loop_heartbeat import beat

logger = logging.getLogger(__name__)

_WORLD_EVENT_SOURCE = "world_event"
_TRIVIA_TIER = "trivia"


def _missing_embedding_predicates():
    """Shared queue predicate for selection and progress measurement.

    Explicit world-event trivia is intentionally vectorless: it is excluded
    from the retrieval lane and embedding it would spend capacity without
    improving recall.  Untagged legacy world events remain eligible.
    """
    tier = Memory.metadata_json["tier"].as_string()
    not_explicit_trivia = or_(
        Memory.source != _WORLD_EVENT_SOURCE,
        tier.is_(None),
        tier != _TRIVIA_TIER,
    )
    return (
        Memory.type == "event",
        Memory.embedding.is_(None),
        Memory.archived_at.is_(None),
        not_explicit_trivia,
    )


async def _backlog_snapshot(db: AsyncSession) -> tuple[int, object | None]:
    row = (await db.execute(
        select(func.count(), func.min(Memory.created_at)).where(
            *_missing_embedding_predicates()
        )
    )).one()
    return int(row[0] or 0), row[1]


async def cleanup_zero_embeddings(db: AsyncSession) -> int:
    """Set all-zero embeddings back to NULL. Returns number of rows cleaned."""
    if db.get_bind().dialect.name == "postgresql":
        # Fast path: inner product of a vector with itself is 0 only for the zero vector
        result = await db.execute(text(
            "UPDATE memories SET embedding = NULL "
            "WHERE type = 'event' AND embedding IS NOT NULL "
            "AND embedding <#> embedding = 0"
        ))
        await db.commit()
        return result.rowcount or 0

    # Portable path (SQLite dev/test): scan in Python
    result = await db.execute(
        select(Memory).where(Memory.type == "event", Memory.embedding.is_not(None))
    )
    cleaned = 0
    for mem in result.scalars().all():
        if mem.embedding and not any(mem.embedding):
            mem.embedding = None
            cleaned += 1
    if cleaned:
        await db.commit()
    return cleaned


async def backfill_missing_embeddings(
    db: AsyncSession,
    batch_size: int | None = None,
    request_size: int | None = None,
) -> int:
    """Recompute embeddings for event memories with NULL embedding.

    Rows stay NULL if the provider is unavailable (retried next round).
    Returns number of rows fixed.
    """
    from app.config import settings

    if batch_size is None:
        batch_size = settings.embedding_backfill_batch_size
    if request_size is None:
        request_size = settings.embedding_backfill_request_size
    started = time.monotonic()
    result = await db.execute(
        select(Memory)
        .where(*_missing_embedding_predicates())
        # FIFO is essential: DESC permanently starved every row older than the
        # live write rate's newest batch.
        .order_by(Memory.created_at.asc(), Memory.id.asc())
        .limit(batch_size)
    )
    rows = list(result.scalars().all())
    fixed = 0
    for offset in range(0, len(rows), request_size):
        chunk = rows[offset:offset + request_size]
        try:
            embeddings = await generate_embeddings_batch([m.content for m in chunk])
        except Exception:
            # The client is already fail-open, but this protects the queue from
            # patched/provider-adapter bugs outside its normal failure contract.
            logger.exception(
                "EMBEDDING_BACKFILL_CHUNK_FAILED offset=%d size=%d",
                offset,
                len(chunk),
            )
            embeddings = [None] * len(chunk)
        for mem, embedding in zip(chunk, embeddings):
            if embedding is not None:
                mem.embedding = embedding
                fixed += 1
    if fixed:
        await db.commit()
    remaining, oldest = await _backlog_snapshot(db)
    elapsed_ms = round((time.monotonic() - started) * 1000)
    selected = len(rows)
    failed = selected - fixed
    log = logger.warning if selected and fixed == 0 else logger.info
    log(
        "EMBEDDING_BACKFILL_ROUND selected=%d fixed=%d failed=%d "
        "remaining=%d oldest_missing_at=%s duration_ms=%d db_batch=%d request_batch=%d",
        selected,
        fixed,
        failed,
        remaining,
        oldest.isoformat() if oldest is not None else "none",
        elapsed_ms,
        batch_size,
        request_size,
    )
    return fixed


async def embedding_backfill_loop() -> None:
    """Background task: clean zero-vectors once, then drain the NULL queue."""
    from app.config import settings
    if not settings.embedding_enabled:
        # No endpoint on this deployment — exit instead of a periodic sweep
        # where every provider call fails (vm212 log spam).
        logger.info("Embedding disabled (EMBEDDING_ENABLED=false); backfill loop not started")
        return
    first_round = True
    while True:
        try:
            async with async_session() as db:
                if first_round:
                    cleaned = await cleanup_zero_embeddings(db)
                    if cleaned:
                        logger.info("Embedding cleanup: nulled %d zero-vectors", cleaned)
                    first_round = False
                await backfill_missing_embeddings(db)
        except Exception as e:
            logger.error("Embedding backfill error: %s", e, exc_info=True)
        # P2: liveness signal + sibling-loop watchdog
        await beat("embedding_backfill")
        await asyncio.sleep(settings.embedding_backfill_interval_seconds)
