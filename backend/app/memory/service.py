import logging
import math
from datetime import datetime, timedelta, UTC
from sqlalchemy import select, func, delete, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.memory import Memory
from app.llm.client import chat as llm_chat
from app.llm.metering import Meter
from app.llm.json_extract import extract_json_object
from app.memory.embedding import generate_embedding
from app.memory.prompts import (
    EXTRACT_EVENTS_SYSTEM,
    EXTRACT_EVENTS_USER,
    UPDATE_RELATIONSHIP_SYSTEM,
    UPDATE_RELATIONSHIP_USER,
    REFLECT_SYSTEM,
    REFLECT_USER,
    CHAT_WRAPUP_SYSTEM,
    CHAT_WRAPUP_USER,
    sbti_coloring_block,
)
from app.personality.evolution import EvolutionService
from app.services.resident_service import resolve_resident_mentions

logger = logging.getLogger(__name__)

# E-28: cap event-memory content length on store (they're re-injected into every
# later retrieval; an unbounded entry is paid for repeatedly).
EVENT_MEMORY_MAX_CHARS = 80


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity of two equal-length vectors; 0.0 if either is missing."""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = num_a = num_b = 0.0
    for i in range(n):
        av = float(a[i]); bv = float(b[i])
        dot += av * bv
        num_a += av * av
        num_b += bv * bv
    if num_a == 0.0 or num_b == 0.0:
        return 0.0
    return dot / (math.sqrt(num_a) * math.sqrt(num_b))


class MemoryService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def add_memory(
        self,
        resident_id: str,
        type: str,
        content: str,
        importance: float,
        source: str,
        *,
        related_resident_id: str | None = None,
        related_user_id: str | None = None,
        media_url: str | None = None,
        media_summary: str | None = None,
        embedding: list[float] | None = None,
        metadata_json: dict | None = None,
    ) -> Memory:
        """Create and persist a new memory record."""
        # E-28: event memories are re-injected into every later retrieval, so an
        # unbounded entry is paid for over and over. Cap them on store. Longer,
        # bounded-count types (relationship/reflection) keep their full text.
        if type == "event" and content and len(content) > EVENT_MEMORY_MAX_CHARS:
            content = content[:EVENT_MEMORY_MAX_CHARS].rstrip() + "…"
        mem = Memory(
            resident_id=resident_id,
            type=type,
            content=content,
            importance=importance,
            source=source,
            related_resident_id=related_resident_id,
            related_user_id=related_user_id,
            media_url=media_url,
            media_summary=media_summary,
            embedding=embedding,
            metadata_json=metadata_json,
        )
        self.db.add(mem)
        await self.db.commit()
        await self.db.refresh(mem)
        # S2/D1: a memory that references a player fires the domain event that
        # drives the "remembered" / "memory_keeper" achievements. Post-commit;
        # handlers run in their own sessions and never affect this write.
        if related_user_id:
            from app.events.bus import emit
            await emit(self.db, "memory_written_about_user",
                       user_id=related_user_id, resident_id=resident_id, memory_id=mem.id)
        return mem

    async def get_memories(
        self,
        resident_id: str,
        *,
        type: str | None = None,
        limit: int = 50,
    ) -> list[Memory]:
        """Get memories for a resident, optionally filtered by type."""
        stmt = select(Memory).where(Memory.resident_id == resident_id)
        if type:
            stmt = stmt.where(Memory.type == type)
        stmt = stmt.order_by(Memory.created_at.desc()).limit(limit)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_relationship(
        self,
        resident_id: str,
        *,
        user_id: str | None = None,
        resident_id_target: str | None = None,
    ) -> Memory | None:
        """Get the relationship memory for a specific person."""
        stmt = select(Memory).where(
            Memory.resident_id == resident_id,
            Memory.type == "relationship",
        )
        if user_id:
            stmt = stmt.where(Memory.related_user_id == user_id)
        elif resident_id_target:
            stmt = stmt.where(Memory.related_resident_id == resident_id_target)
        else:
            return None
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def update_relationship(
        self,
        resident_id: str,
        *,
        user_id: str | None = None,
        resident_id_target: str | None = None,
        content: str,
        importance: float,
        metadata_json: dict | None = None,
    ) -> Memory:
        """Update an existing relationship memory, or create if not found."""
        existing = await self.get_relationship(
            resident_id, user_id=user_id, resident_id_target=resident_id_target,
        )
        if existing:
            existing.content = content
            existing.importance = importance
            if metadata_json is not None:
                existing.metadata_json = metadata_json
            existing.last_accessed_at = datetime.now(UTC)
            await self.db.commit()
            return existing
        else:
            return await self.add_memory(
                resident_id, "relationship", content, importance,
                "chat_player" if user_id else "chat_resident",
                related_user_id=user_id,
                related_resident_id=resident_id_target,
                metadata_json=metadata_json,
            )

    async def get_recent_reflections(
        self,
        resident_id: str,
        limit: int = 5,
    ) -> list[Memory]:
        """Get most important recent reflections."""
        stmt = (
            select(Memory)
            .where(Memory.resident_id == resident_id, Memory.type == "reflection")
            .order_by(Memory.importance.desc(), Memory.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def count_events_since_last_reflection(self, resident_id: str) -> int:
        """Count event memories created after the most recent reflection."""
        last_ref_stmt = (
            select(Memory.created_at)
            .where(Memory.resident_id == resident_id, Memory.type == "reflection")
            .order_by(Memory.created_at.desc())
            .limit(1)
        )
        result = await self.db.execute(last_ref_stmt)
        last_ref_time = result.scalar_one_or_none()

        count_stmt = select(func.count()).select_from(Memory).where(
            Memory.resident_id == resident_id,
            Memory.type == "event",
        )
        if last_ref_time:
            count_stmt = count_stmt.where(Memory.created_at > last_ref_time)

        result = await self.db.execute(count_stmt)
        return result.scalar_one()

    async def evict_memories(
        self,
        resident_id: str,
        *,
        importance_floor: float | None = None,
        idle_days: int | None = None,
    ) -> int:
        """Soft-archive stale, low-importance *event* memories (realism P0-2).

        Score-floor semantics (replaces the old hard 500-count cap): an event
        memory with ``importance < floor`` that hasn't been accessed in
        ``idle_days`` is marked ``archived_at`` (kept for provenance, excluded
        from active retrieval). relationship/reflection/dream memories are never
        archived (only ``type == "event"`` is considered).
        """
        from app.config import settings
        floor = settings.realism_evict_importance_floor if importance_floor is None else importance_floor
        days = settings.realism_evict_idle_days if idle_days is None else idle_days
        cutoff = datetime.now(UTC) - timedelta(days=days)
        stmt = select(Memory).where(
            Memory.resident_id == resident_id,
            Memory.type == "event",
            Memory.importance < floor,
            Memory.last_accessed_at < cutoff,
            Memory.archived_at.is_(None),
        )
        rows = list((await self.db.execute(stmt)).scalars().all())
        if rows:
            now = datetime.now(UTC)
            for m in rows:
                m.archived_at = now
            await self.db.commit()
        return len(rows)

    async def retrieve_context(
        self,
        resident_id: str,
        *,
        user_id: str | None = None,
        resident_id_target: str | None = None,
        query_text: str = "",
        max_events: int = 10,
        max_reflections: int = 3,
    ) -> dict:
        """Retrieve memory context for a conversation.

        Returns dict with keys: relationship, reflections, events.
        """
        # 1. Structured: relationship memory for this person
        relationship = await self.get_relationship(
            resident_id, user_id=user_id, resident_id_target=resident_id_target,
        )

        # 2. Structured: top reflections by importance
        reflections = await self.get_recent_reflections(resident_id, limit=max_reflections)

        # 3. Events: realism scored vector retrieval, else recency+importance
        events = await self._retrieve_events(resident_id, query_text, max_events)

        # Update last_accessed_at for all retrieved memories
        now = datetime.now(UTC)
        all_memories = [m for m in [relationship] + reflections + events if m is not None]
        for mem in all_memories:
            mem.last_accessed_at = now
        if all_memories:
            await self.db.commit()

        return {
            "relationship": relationship,
            "reflections": reflections,
            "events": events,
        }

    async def _fetch_event_candidates(self, resident_id: str, cap: int) -> list[Memory]:
        """Top-`cap` non-archived event memories by static importance/recency."""
        stmt = (
            select(Memory)
            .where(
                Memory.resident_id == resident_id,
                Memory.type == "event",
                Memory.archived_at.is_(None),
            )
            .order_by(Memory.importance.desc(), Memory.created_at.desc())
            .limit(cap)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def _search_events(
        self,
        resident_id: str,
        query_text: str,
        limit: int = 10,
    ) -> list[Memory]:
        """Search event memories. Falls back to recency+importance ranking."""
        return await self._fetch_event_candidates(resident_id, limit)

    async def _retrieve_events(
        self,
        resident_id: str,
        query_text: str,
        limit: int,
    ) -> list[Memory]:
        """Realism P0-2: scored vector retrieval when enabled + query + embedding
        available; else static importance/recency (fail-open)."""
        from app.config import settings
        if settings.realism_enabled and query_text:
            try:
                emb = await generate_embedding(query_text)
                if emb:
                    return await self._search_events_scored(resident_id, emb, limit)
            except Exception as e:
                logger.debug("scored retrieval fell back: %s", e)
        return await self._search_events(resident_id, "", limit)

    async def _search_events_scored(
        self,
        resident_id: str,
        query_embedding: list[float],
        limit: int = 10,
    ) -> list[Memory]:
        """Rank candidates by Generative-Agents weighting:
        ``0.45×relevance + 0.30×recency + 0.25×importance`` where
        ``recency = exp(-Δt_hours / τ)``, ``τ = 72h × (1 + importance)``.

        Cosine relevance is computed in Python over the top-K static candidates
        (works identically on sqlite tests and pgvector prod; no dead-code path).
        """
        from app.config import settings
        candidates = await self._fetch_event_candidates(resident_id, cap=max(limit * 3, 30))
        if not candidates:
            return []
        now = datetime.now(UTC)
        wr = settings.realism_retrieval_relevance_weight
        wc = settings.realism_retrieval_recency_weight
        wi = settings.realism_retrieval_importance_weight
        tau_base = settings.realism_recency_tau_hours
        scored: list[tuple[float, Memory]] = []
        for m in candidates:
            imp = m.importance or 0.0
            rel = _cosine(query_embedding, m.embedding)
            created = m.created_at if m.created_at.tzinfo else m.created_at.replace(tzinfo=UTC)
            dt_h = max((now - created).total_seconds() / 3600.0, 0.0)
            tau = tau_base * (1.0 + imp)
            recency = math.exp(-dt_h / tau) if tau > 0 else 0.0
            scored.append((wr * rel + wc * recency + wi * imp, m))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [m for _, m in scored[:limit]]

    async def search_events_vector(
        self,
        resident_id: str,
        query_embedding: list[float],
        limit: int = 10,
    ) -> list[Memory]:
        """Search event memories using pgvector cosine similarity.

        For PostgreSQL with pgvector only. Falls back to _search_events() if unavailable.
        """
        try:
            from sqlalchemy import text
            stmt = text("""
                SELECT id, content, importance, source, created_at, last_accessed_at,
                       metadata_json, media_url, media_summary,
                       1 - (embedding <=> :query_vec) AS similarity
                FROM memories
                WHERE resident_id = :rid AND type = 'event' AND embedding IS NOT NULL
                      AND archived_at IS NULL
                ORDER BY embedding <=> :query_vec
                LIMIT :lim
            """)
            result = await self.db.execute(stmt, {
                "rid": resident_id,
                "query_vec": str(query_embedding),
                "lim": limit,
            })
            rows = result.fetchall()
            if not rows:
                return await self._search_events(resident_id, "", limit)

            ids = [row[0] for row in rows]
            mem_stmt = select(Memory).where(Memory.id.in_(ids))
            mem_result = await self.db.execute(mem_stmt)
            memories = {m.id: m for m in mem_result.scalars().all()}
            return [memories[id] for id in ids if id in memories]
        except Exception as e:
            logger.debug("pgvector search unavailable, falling back: %s", e)
            return await self._search_events(resident_id, "", limit)

    async def extract_events(
        self,
        resident: "Resident",
        other_name: str,
        conversation_text: str,
        *,
        source: str = "chat_player",
    ) -> list[Memory]:
        """Extract event memories from a conversation using LLM."""
        sbti_data = (resident.meta_json or {}).get("sbti")
        coloring = sbti_coloring_block(sbti_data)

        system = EXTRACT_EVENTS_SYSTEM.format(sbti_coloring=coloring)
        user_msg = EXTRACT_EVENTS_USER.format(
            resident_name=resident.name,
            other_name=other_name,
            conversation_text=conversation_text,
        )

        try:
            raw = await llm_chat(
                system, [{"role": "user", "content": user_msg}], max_tokens=500,
                meter=Meter(scenario="extract", resident_id=resident.id), expects_json=True,
            )
            data = extract_json_object(raw)
            if data is None:
                raise ValueError("no JSON object in LLM response")
        except Exception as e:
            logger.warning("Event extraction failed: %s", e)
            return []

        items = data.get("memories", [])
        mentioned = [i.get("mentioned_resident") for i in items
                     if isinstance(i, dict) and i.get("mentioned_resident")]
        mention_map = await resolve_resident_mentions(self.db, mentioned) if mentioned else {}

        memories = []
        for item in items:
            content = item.get("content", "")
            importance = float(item.get("importance", 0.5))
            if not content:
                continue

            related_id = mention_map.get((item.get("mentioned_resident") or "").strip())
            if related_id == resident.id:
                related_id = None  # 不指向自己

            emb = await generate_embedding(content)
            mem = await self.add_memory(
                resident_id=resident.id,
                type="event",
                content=content,
                importance=importance,
                source=source,
                embedding=emb,
                related_resident_id=related_id,
            )
            memories.append(mem)

        # Evolution hooks (non-blocking)
        await self._run_evolution_hooks(resident, memories)

        return memories

    async def _run_evolution_hooks(self, resident: "Resident", memories: list[Memory]) -> None:
        """Conditionally trigger personality shift/drift from freshly-extracted
        event memories. Shared by extract_events and process_chat_wrapup."""
        if not memories or resident is None:
            return
        evo = EvolutionService(self.db)

        # Check for shift on any high-importance memory
        high_importance = [m for m in memories if m.importance >= 0.9]
        if high_importance:
            try:
                await evo.evaluate_shift(resident, high_importance[0])
            except Exception as e:
                logger.warning("Shift evaluation error (non-fatal): %s", e)

        # Check drift trigger: count total events since last drift
        total_events = await self.count_events_since_last_reflection(resident.id)
        if total_events >= 15:
            try:
                await evo.evaluate_drift(resident)
            except Exception as e:
                logger.warning("Drift evaluation error (non-fatal): %s", e)

    async def process_chat_wrapup(
        self,
        initiator: "Resident",
        target: "Resident",
        dialog_text: str,
    ) -> dict:
        """One-call resident-resident chat wrap-up (E-04/E-05).

        Replaces the old five wrap-up LLM calls (extract×2 + update_relationship×2
        + summary) with a single merged call — the dialog is sent once instead of
        five times. Retries once on parse failure, then falls back to a generic
        summary (never blank-screens). Persists both residents' event memories
        (with embeddings + evolution hooks) and relationship updates, and returns
        the broadcast ``{summary, mood}``.
        """
        init_rel = await self.get_relationship(initiator.id, resident_id_target=target.id)
        tgt_rel = await self.get_relationship(target.id, resident_id_target=initiator.id)
        user_msg = CHAT_WRAPUP_USER.format(
            initiator_name=initiator.name,
            target_name=target.name,
            initiator_relationship=(init_rel.content if init_rel else "（首次接触，尚无关系记忆）"),
            target_relationship=(tgt_rel.content if tgt_rel else "（首次接触，尚无关系记忆）"),
            conversation_text=dialog_text,
        )

        data = None
        for attempt in (1, 2):  # E-05: one retry on parse failure
            try:
                raw = await llm_chat(
                    CHAT_WRAPUP_SYSTEM, [{"role": "user", "content": user_msg}], max_tokens=800,
                    meter=Meter(scenario="chat_wrapup", resident_id=initiator.id, attempt_no=attempt),
                    expects_json=True,
                )
                data = extract_json_object(raw)
            except Exception as e:
                logger.warning("Chat wrap-up call failed (attempt %d): %s", attempt, e)
                data = None
            if data is not None:
                break

        fallback = {"summary": f"{initiator.name} 和 {target.name} 聊了一会儿", "mood": "neutral"}
        if data is None:
            return fallback

        init_side = data.get("initiator") or {}
        tgt_side = data.get("target") or {}
        mentioned = [
            i.get("mentioned_resident")
            for side in (init_side, tgt_side)
            for i in (side.get("memories") or [])
            if isinstance(i, dict) and i.get("mentioned_resident")
        ]
        mention_map = await resolve_resident_mentions(self.db, mentioned) if mentioned else {}

        await self._persist_wrapup_side(initiator, target, init_side, mention_map)
        await self._persist_wrapup_side(target, initiator, tgt_side, mention_map)

        mood = data.get("mood")
        return {
            "summary": str(data.get("summary") or fallback["summary"]),
            "mood": mood if mood in ("positive", "neutral", "negative") else "neutral",
        }

    async def _persist_wrapup_side(
        self, resident: "Resident", other: "Resident", side: dict,
        mention_map: dict[str, str] | None = None,
    ) -> None:
        """Persist one resident's extracted memories + relationship from the
        merged wrap-up result, then run evolution hooks."""
        memories: list[Memory] = []
        for item in (side.get("memories") or []):
            content = (item.get("content") or "").strip()
            if not content:
                continue
            importance = float(item.get("importance", 0.5))
            raw_mention = (item.get("mentioned_resident") or "").strip()
            related_id = (mention_map or {}).get(raw_mention)
            if not related_id or related_id == resident.id:
                related_id = other.id  # 默认：记忆关于对话对象
            emb = await generate_embedding(content)
            mem = await self.add_memory(
                resident_id=resident.id, type="event", content=content,
                importance=importance, source="chat_resident", embedding=emb,
                related_resident_id=related_id,
            )
            memories.append(mem)

        rel = side.get("relationship") or {}
        rel_content = (rel.get("content") or "").strip() if isinstance(rel, dict) else ""
        if rel_content:
            meta = rel.get("metadata")
            await self.update_relationship(
                resident.id, resident_id_target=other.id,
                content=rel_content, importance=float(rel.get("importance", 0.5)),
                metadata_json=meta if isinstance(meta, dict) else None,
            )

        await self._run_evolution_hooks(resident, memories)

    async def update_relationship_via_llm(
        self,
        resident: "Resident",
        other_name: str,
        event_summaries: list[str],
        *,
        user_id: str | None = None,
        resident_id_target: str | None = None,
    ) -> Memory:
        """Update relationship memory using LLM analysis."""
        existing = await self.get_relationship(
            resident.id, user_id=user_id, resident_id_target=resident_id_target,
        )
        current_rel = existing.content if existing else "（首次接触，尚无关系记忆）"

        sbti_data = (resident.meta_json or {}).get("sbti")
        coloring = sbti_coloring_block(sbti_data)

        system = UPDATE_RELATIONSHIP_SYSTEM.format(sbti_coloring=coloring)
        user_msg = UPDATE_RELATIONSHIP_USER.format(
            resident_name=resident.name,
            other_name=other_name,
            current_relationship=current_rel,
            event_summaries="\n".join(f"- {s}" for s in event_summaries),
        )

        try:
            raw = await llm_chat(
                system, [{"role": "user", "content": user_msg}], max_tokens=300,
                meter=Meter(scenario="update_rel", resident_id=resident.id), expects_json=True,
            )
            data = extract_json_object(raw)
            if data is None:
                raise ValueError("no JSON object in LLM response")
        except Exception as e:
            logger.warning("Relationship update failed: %s", e)
            if existing:
                return existing
            return await self.add_memory(
                resident.id, "relationship", f"Met {other_name}",
                0.3, "chat_player" if user_id else "chat_resident",
                related_user_id=user_id, related_resident_id=resident_id_target,
            )

        return await self.update_relationship(
            resident.id,
            user_id=user_id,
            resident_id_target=resident_id_target,
            content=data.get("content", f"Met {other_name}"),
            importance=float(data.get("importance", 0.5)),
            metadata_json=data.get("metadata"),
        )

    async def generate_reflections(self, resident: "Resident") -> list[Memory]:
        """Generate reflection memories from recent events and relationships."""
        recent_events = await self.get_memories(resident.id, type="event", limit=20)
        relationships = await self.get_memories(resident.id, type="relationship", limit=10)

        if not recent_events:
            return []

        sbti_data = (resident.meta_json or {}).get("sbti")
        coloring = sbti_coloring_block(sbti_data)

        events_text = "\n".join(f"- [{e.source}] {e.content}" for e in recent_events)
        rels_text = "\n".join(f"- {r.content}" for r in relationships) if relationships else "（尚无关系记忆）"

        system = REFLECT_SYSTEM.format(sbti_coloring=coloring)
        user_msg = REFLECT_USER.format(
            resident_name=resident.name,
            recent_events=events_text,
            relationships=rels_text,
        )

        try:
            raw = await llm_chat(
                system, [{"role": "user", "content": user_msg}], max_tokens=400,
                meter=Meter(scenario="reflect", resident_id=resident.id), expects_json=True,
            )
            data = extract_json_object(raw)
            if data is None:
                raise ValueError("no JSON object in LLM response")
        except Exception as e:
            logger.warning("Reflection generation failed: %s", e)
            return []

        reflections = []
        for item in data.get("reflections", []):
            content = item.get("content", "")
            importance = float(item.get("importance", 0.6))
            if not content:
                continue
            mem = await self.add_memory(
                resident.id, "reflection", content, importance, "reflection",
            )
            reflections.append(mem)

        return reflections
