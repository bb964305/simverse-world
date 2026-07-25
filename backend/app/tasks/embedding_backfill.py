"""Compensation task for memory embeddings (P0-5).

Legacy code wrote zero-vectors on Ollama failures, which poison
cosine-distance retrieval (distance to a zero vector is NaN). This task:
1. Nulls out any remaining all-zero embeddings (one-off data cleanup)
2. Periodically recomputes embeddings for event memories left at NULL
"""
import asyncio
import logging

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.memory.embedding import generate_embedding
from app.models.memory import Memory
from app.tasks.loop_heartbeat import beat

logger = logging.getLogger(__name__)
BACKFILL_INTERVAL_SECONDS = 3600  # 1 hour
BACKFILL_BATCH_SIZE = 50


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
    db: AsyncSession, batch_size: int = BACKFILL_BATCH_SIZE
) -> int:
    """Recompute embeddings for event memories with NULL embedding.

    Rows stay NULL if Ollama is still unavailable (retried next round).
    Returns number of rows fixed.
    """
    result = await db.execute(
        select(Memory)
        .where(Memory.type == "event", Memory.embedding.is_(None))
        .order_by(Memory.created_at.desc())
        .limit(batch_size)
    )
    fixed = 0
    for mem in result.scalars().all():
        emb = await generate_embedding(mem.content)
        if emb is not None:
            mem.embedding = emb
            fixed += 1
    if fixed:
        await db.commit()
    return fixed


async def embedding_backfill_loop() -> None:
    """Background task: clean zero-vectors once, then backfill NULLs hourly."""
    from app.config import settings
    if not settings.embedding_enabled:
        # No endpoint on this deployment — exit instead of an hourly sweep
        # where every generate_embedding() call fails (vm212 log spam).
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
                fixed = await backfill_missing_embeddings(db)
                if fixed:
                    logger.info("Embedding backfill: recomputed %d embeddings", fixed)
        except Exception as e:
            logger.error("Embedding backfill error: %s", e, exc_info=True)
        # P2: liveness signal + sibling-loop watchdog
        await beat("embedding_backfill")
        await asyncio.sleep(BACKFILL_INTERVAL_SECONDS)
