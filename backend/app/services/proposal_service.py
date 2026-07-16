"""WorldChangeProposal lifecycle (spec §7): create → review → apply/revert.

Fuel: cost_sc is frozen from the author's treasury on create, consumed on apply,
refunded on reject/failed apply. Apply always goes through admin review — no
whitelist auto-apply (spec §7.5). Risk is rule-assessed (an LLM hook is left as
a P4 refinement).
"""
from __future__ import annotations

import logging
from datetime import datetime, UTC

from sqlalchemy import select

from app.database import async_session
from app.events.bus import on, emit
from app.lab import apply as apply_engine
from app.models.world_change_proposal import WorldChangeProposal
from app.models.resident import Resident
from app.services import coin_service

logger = logging.getLogger(__name__)

OPEN_KINDS = ("add_lore", "edit_location", "add_location", "add_mechanic")  # add_npc deferred (P4)


class ProposalError(Exception):
    """Create/approve/reject/revert conflicts (router maps to 400/402/409)."""


def assess_risk(kind: str, patch: dict) -> str:
    if kind in ("add_npc", "edit_npc"):
        return "high"
    if kind in ("add_location", "add_mechanic"):
        return "medium"
    return "low"  # add_lore, edit_location


def serialize(p: WorldChangeProposal) -> dict:
    return {
        "id": p.id,
        "origin": p.origin,
        "origin_ref": p.origin_ref,
        "author_slug": p.author_slug,
        "kind": p.kind,
        "title": p.title,
        "rationale_md": p.rationale_md,
        "patch": p.patch_json or {},
        "cost_sc": p.cost_sc,
        "status": p.status,
        "risk_level": p.risk_level,
        "reviewer_id": p.reviewer_id,
        "review_note": p.review_note,
        "applied_at": p.applied_at.isoformat() if p.applied_at else None,
        "reverted_at": p.reverted_at.isoformat() if p.reverted_at else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


async def create_proposal(
    db, *, kind: str, title: str, rationale: str, patch: dict,
    origin: str = "lab_run", origin_ref: str | None = None, author_slug: str | None = None,
    cost_sc: int = 0, risk_level: str | None = None,
) -> WorldChangeProposal:
    if kind not in OPEN_KINDS:
        raise ProposalError(f"proposal kind '{kind}' is not open")
    # Freeze treasury fuel (refunded on reject / failed apply).
    if cost_sc > 0 and author_slug:
        ok = await coin_service.treasury_debit(db, author_slug, cost_sc, f"proposal_fuel:{title}")
        if not ok:
            raise ProposalError("insufficient treasury fuel")
    p = WorldChangeProposal(
        origin=origin, origin_ref=origin_ref, author_slug=author_slug, kind=kind,
        title=title[:200], rationale_md=rationale or "", patch_json=patch or {},
        cost_sc=cost_sc, risk_level=risk_level or assess_risk(kind, patch or {}), status="pending",
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def approve_proposal(db, proposal_id: str, reviewer_id: str, note: str = "") -> WorldChangeProposal:
    p = await db.get(WorldChangeProposal, proposal_id)
    if p is None:
        raise ProposalError("proposal not found")
    if p.status != "pending":
        raise ProposalError("proposal is not pending")
    p.status = "approved"
    p.reviewer_id = reviewer_id
    p.review_note = note or p.review_note
    await db.commit()
    try:
        await apply_engine.apply_proposal(db, p)
    except apply_engine.ApplyError as e:
        p.status = "failed"
        p.review_note = f"{note or ''} | apply failed: {e}".strip(" |")
        await db.commit()
        if p.cost_sc > 0 and p.author_slug:
            await coin_service.treasury_credit(db, p.author_slug, p.cost_sc, f"proposal_refund:{p.id}")
        raise ProposalError(f"apply failed: {e}")
    p.status = "applied"
    p.applied_at = datetime.now(UTC)
    await db.commit()
    await emit(db, "world_proposal_applied", proposal_id=p.id, author_slug=p.author_slug, kind=p.kind)
    await db.refresh(p)
    return p


async def reject_proposal(db, proposal_id: str, reviewer_id: str, note: str = "") -> WorldChangeProposal:
    p = await db.get(WorldChangeProposal, proposal_id)
    if p is None:
        raise ProposalError("proposal not found")
    if p.status != "pending":
        raise ProposalError("proposal is not pending")
    if p.cost_sc > 0 and p.author_slug:
        await coin_service.treasury_credit(db, p.author_slug, p.cost_sc, f"proposal_refund:{p.id}")
    p.status = "rejected"
    p.reviewer_id = reviewer_id
    p.review_note = note or p.review_note
    await db.commit()
    await db.refresh(p)
    return p


async def revert_proposal(db, proposal_id: str, reviewer_id: str) -> WorldChangeProposal:
    p = await db.get(WorldChangeProposal, proposal_id)
    if p is None:
        raise ProposalError("proposal not found")
    if p.status != "applied":
        raise ProposalError("only applied proposals can be reverted")
    await apply_engine.revert_proposal(db, p)
    p.status = "reverted"
    p.reverted_at = datetime.now(UTC)
    p.reviewer_id = reviewer_id
    await db.commit()
    await db.refresh(p)
    return p


@on("world_proposal_applied")
async def _on_proposal_applied(db, proposal_id: str = "", author_slug: str | None = None,
                               kind: str = "", **kw) -> None:
    """The researcher remembers changing the world — a high-importance event."""
    if not author_slug:
        return
    async with async_session() as s:
        res = (await s.execute(select(Resident).where(Resident.slug == author_slug))).scalar_one_or_none()
        if res is None:
            return
        try:
            from app.memory.service import MemoryService
            await MemoryService(s).add_memory(
                res.id, "event", "我提出的世界变更通过了审核，小镇因我而改变了",
                importance=0.9, source="world_proposal",
            )
        except Exception:
            logger.warning("proposal-applied memory failed for %s", author_slug, exc_info=True)
