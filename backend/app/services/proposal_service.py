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
from app.lab import transitions
from app.models.world_change_proposal import WorldChangeProposal
from app.models.resident import Resident
from app.services import coin_service
from app.services import world_revision_service

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


async def _fail_apply(db, p: WorldChangeProposal, note: str, reason: str) -> None:
    """Shared "apply didn't happen" bookkeeping for both a structural
    ApplyError and a revision-capture/stale-base failure: proposal -> failed,
    fuel refunded. No commit has touched the overlay tables at this point for
    either failure kind, so this is the single rollback-equivalent path."""
    p.status = "failed"
    p.review_note = f"{note or ''} | apply failed: {reason}".strip(" |")
    await db.commit()
    # Content-free alert: only the proposal id + a fixed reason CODE (never the
    # raw failure text, which could carry content).
    from app.lab import telemetry
    telemetry.emit_alert(
        telemetry.LabAlert.WORLD_APPLY_FAILED, proposal_id=p.id, reason="apply_failed",
    )
    if p.cost_sc > 0 and p.author_slug:
        await coin_service.treasury_credit(db, p.author_slug, p.cost_sc, f"proposal_refund:{p.id}")


async def approve_proposal(db, proposal_id: str, reviewer_id: str, note: str = "") -> WorldChangeProposal:
    p = await db.get(WorldChangeProposal, proposal_id)
    if p is None:
        raise ProposalError("proposal not found")
    if p.status != "pending":
        raise ProposalError("proposal is not pending")
    # CAS pending -> approved so two racing admins cannot both proceed to apply
    # the same proposal (which would double-write the overlay). The loser sees
    # rowcount 0 and is rejected.
    won = await transitions.cas_proposal_status(
        db, proposal_id=proposal_id, expected=("pending",), new="approved",
        reviewer_id=reviewer_id, review_note=(note or p.review_note),
    )
    await db.commit()
    if not won:
        raise ProposalError("proposal is not pending")
    await db.refresh(p)

    # v1 world-revision tracking is scoped to add_lore/edit_location
    # (global-constraints.md #4); add_location/add_mechanic keep the
    # pre-T6 apply/broadcast path byte-for-byte (test_world_governance.py).
    revisioned = p.kind in world_revision_service.REVISIONED_KINDS
    before_state = base_revision_id = None
    if revisioned:
        location_slug = world_revision_service.location_slug_for(p.kind, p.patch_json or {})
        base_revision_id = await world_revision_service.current_revision_id(db, location_slug)
        requested_base = (p.patch_json or {}).get("base_world_revision")
        if requested_base is not None and requested_base != base_revision_id:
            await _fail_apply(db, p, note, "stale base revision")
            raise ProposalError("stale base revision")
        try:
            before_state = await world_revision_service.capture_before_state(
                db, kind=p.kind, patch=p.patch_json or {},
            )
        except world_revision_service.RevisionError as e:
            await _fail_apply(db, p, note, str(e))
            raise ProposalError(f"apply failed: {e}")

    try:
        # Revisioned kinds flush-only (commit=False): the single commit below is
        # the atomic boundary. Non-revisioned kinds keep the legacy commit-inside
        # + own reload/broadcast.
        await apply_engine.apply_proposal(
            db, p, broadcast=not revisioned, commit=not revisioned)
    except apply_engine.ApplyError as e:
        # A validation/conflict failure precedes any overlay flush, but roll back
        # defensively so no partially-flushed overlay can ride the _fail_apply
        # commit, then re-load the (expired) proposal to mark it failed.
        await db.rollback()
        p = await db.get(WorldChangeProposal, proposal_id)
        await _fail_apply(db, p, note, str(e))
        raise ProposalError(f"apply failed: {e}")

    envelope = None
    if revisioned:
        # Same read as before_state, taken again now that the write landed (still
        # in-session, visible via the flush) — "the target's current state" is the
        # after-image this time.
        after_state = await world_revision_service.capture_before_state(
            db, kind=p.kind, patch=p.patch_json or {},
        )
        revision = await world_revision_service.record_apply(
            db, proposal=p, before_state=before_state, after_state=after_state,
            base_revision_id=base_revision_id, applied_by=reviewer_id,
        )
        envelope = await world_revision_service.build_world_changed_envelope(
            db, revision=revision, action="applied", tenant_id=revision.tenant_id,
        )

    p.status = "applied"
    p.applied_at = datetime.now(UTC)
    # ONE transaction: the flushed overlay + immutable revision + outbox record +
    # proposal terminal status commit together, so a crash before here leaves no
    # overlay split from its audit. Reload/broadcast happen AFTER the commit — a
    # separate-session reload can only see the committed overlay.
    await db.commit()
    if revisioned:
        await apply_engine.reload_world()
        await apply_engine.publish_world_reload()
    if envelope is not None:
        await apply_engine.broadcast_world_changed(payload=envelope)
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

    revision = None
    if p.kind in world_revision_service.REVISIONED_KINDS:
        revision = await world_revision_service.latest_applied_revision(db, proposal_id=p.id)

    if revision is not None:
        # Before-state restore path (T6): exact-slug data_json/lore rollback,
        # not the pre-T6 soft-delete. The restore, the revision->reverted flip,
        # the outbox row, and the proposal status commit in ONE transaction so a
        # crash cannot revert the audit without the overlay (or vice versa);
        # reload_world (its own session) only runs AFTER the commit.
        await world_revision_service.revert_revision(db, revision=revision, reverted_by=reviewer_id)
        envelope = await world_revision_service.build_world_changed_envelope(
            db, revision=revision, action="reverted", tenant_id=revision.tenant_id,
        )
        # CAS applied -> reverted: two racing reverts cannot both land; the loser
        # rolls back its duplicate restore/outbox and is rejected.
        won = await transitions.cas_proposal_status(
            db, proposal_id=p.id, expected=("applied",), new="reverted",
            reverted_at=datetime.now(UTC), reviewer_id=reviewer_id,
        )
        if not won:
            await db.rollback()
            raise ProposalError("only applied proposals can be reverted")
        await db.commit()  # overlay restore + revision + outbox + status="reverted" together
        await apply_engine.reload_world()
        await apply_engine.publish_world_reload()
        await apply_engine.broadcast_world_changed(payload=envelope)
    else:
        # No revision on record (add_location/add_mechanic, or a legacy
        # proposal predating T6) — unchanged soft-delete compatibility path,
        # gated by the same CAS so two admins cannot both revert.
        won = await transitions.cas_proposal_status(
            db, proposal_id=p.id, expected=("applied",), new="reverted",
            reverted_at=datetime.now(UTC), reviewer_id=reviewer_id,
        )
        await db.commit()
        if not won:
            raise ProposalError("only applied proposals can be reverted")
        await apply_engine.revert_proposal(db, p)

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
