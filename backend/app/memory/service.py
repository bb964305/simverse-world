import logging
from datetime import datetime, UTC
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

logger = logging.getLogger(__name__)

# E-28: cap event-memory content length on store (they're re-injected into every
# later retrieval; an unbounded entry is paid for repeatedly).
EVENT_MEMORY_MAX_CHARS = 80


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

    async def evict_memories(self, resident_id: str, max_events: int = 500) -> int:
        """Evict oldest, least important event memories beyond the cap."""
        count_result = await self.db.execute(
            select(func.count()).select_from(Memory).where(
                Memory.resident_id == resident_id, Memory.type == "event",
            )
        )
        total = count_result.scalar_one()
        if total <= max_events:
            return 0

        to_evict = total - max_events
        stmt = (
            select(Memory.id)
            .where(Memory.resident_id == resident_id, Memory.type == "event")
            .order_by(Memory.importance.asc(), Memory.last_accessed_at.asc())
            .limit(to_evict)
        )
        result = await self.db.execute(stmt)
        ids_to_delete = [row[0] for row in result.all()]

        if ids_to_delete:
            await self.db.execute(
                delete(Memory).where(Memory.id.in_(ids_to_delete))
            )
            await self.db.commit()

        return len(ids_to_delete)

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

        # 3. Events: try vector search, fall back to recency+importance
        events = await self._search_events(resident_id, query_text, limit=max_events)

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

    async def _search_events(
        self,
        resident_id: str,
        query_text: str,
        limit: int = 10,
    ) -> list[Memory]:
        """Search event memories. Falls back to recency+importance ranking."""
        stmt = (
            select(Memory)
            .where(Memory.resident_id == resident_id, Memory.type == "event")
            .order_by(Memory.importance.desc(), Memory.created_at.desc())
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

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

        memories = []
        for item in data.get("memories", []):
            content = item.get("content", "")
            importance = float(item.get("importance", 0.5))
            if not content:
                continue

            emb = await generate_embedding(content)
            mem = await self.add_memory(
                resident_id=resident.id,
                type="event",
                content=content,
                importance=importance,
                source=source,
                embedding=emb,
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

        await self._persist_wrapup_side(initiator, target, data.get("initiator") or {})
        await self._persist_wrapup_side(target, initiator, data.get("target") or {})

        mood = data.get("mood")
        return {
            "summary": str(data.get("summary") or fallback["summary"]),
            "mood": mood if mood in ("positive", "neutral", "negative") else "neutral",
        }

    async def _persist_wrapup_side(self, resident: "Resident", other: "Resident", side: dict) -> None:
        """Persist one resident's extracted memories + relationship from the
        merged wrap-up result, then run evolution hooks."""
        memories: list[Memory] = []
        for item in (side.get("memories") or []):
            content = (item.get("content") or "").strip()
            if not content:
                continue
            importance = float(item.get("importance", 0.5))
            emb = await generate_embedding(content)
            mem = await self.add_memory(
                resident_id=resident.id, type="event", content=content,
                importance=importance, source="chat_resident", embedding=emb,
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
