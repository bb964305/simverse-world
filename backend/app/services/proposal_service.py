"""WorldChangeProposal lifecycle (spec §7): create → review → apply/revert.

Fuel: cost_sc is frozen from the author's treasury on create, consumed on apply,
refunded on reject/failed apply. Apply always goes through admin review — no
whitelist auto-apply (spec §7.5). Risk is rule-assessed (an LLM hook is left as
a P4 refinement).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, UTC

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

ENABLED_KINDS = ("add_lore", "edit_location")


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
        "global_fencing_epoch": p.global_fencing_epoch,
        "applied_at": p.applied_at.isoformat() if p.applied_at else None,
        "reverted_at": p.reverted_at.isoformat() if p.reverted_at else None,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


def _assert_kind_enabled(kind: str) -> None:
    if kind not in ENABLED_KINDS:
        raise ProposalError(f"proposal kind '{kind}' is not enabled")


async def _current_global_control(db):
    """Load the singleton without importing the P4 control plane at module load.

    The delayed import keeps model registration acyclic. P4 owns the singleton;
    World Governor only binds drafts to its epoch and consumes its lock/check API.
    """
    from app.lab import control_plane

    return await control_plane.ensure_global_control(db)


async def _lock_apply_authority(db, proposal: WorldChangeProposal) -> None:
    """Serialize apply with global kill and reject an old-epoch draft."""
    from app.lab import control_plane

    try:
        await control_plane.assert_effect_epoch(
            db,
            run_id=proposal.origin_ref or proposal.id,
            expected_global_epoch=proposal.global_fencing_epoch,
            effect="world",
        )
    except control_plane.GlobalEffectFenced as exc:
        raise ProposalError(f"stale global epoch: {exc}") from exc


async def _lock_revert_authority(db, proposal: WorldChangeProposal) -> None:
    """A revert is a fresh admin effect, not replay of the proposal's old epoch.

    It still locks the global-control row and is denied while admission is closed,
    but after admission reopens an operator can revert a change that predates the
    latest kill epoch.
    """
    from app.lab import control_plane

    state = await _current_global_control(db)
    try:
        await control_plane.assert_effect_epoch(
            db,
            run_id=proposal.origin_ref or proposal.id,
            expected_global_epoch=state.fencing_epoch,
            effect="world",
        )
    except control_plane.GlobalEffectFenced as exc:
        raise ProposalError(f"world revert fenced: {exc}") from exc


async def create_proposal(
    db, *, kind: str, title: str, rationale: str, patch: dict,
    origin: str = "lab_run", origin_ref: str | None = None, author_slug: str | None = None,
    cost_sc: int = 0, risk_level: str | None = None, commit: bool = True,
) -> WorldChangeProposal:
    _assert_kind_enabled(kind)
    global_control = await _current_global_control(db)
    if not global_control.admission_open:
        raise ProposalError("world proposal admission is closed")
    if not commit and cost_sc > 0:
        raise ProposalError(
            "transaction-owned proposal creation does not support treasury fuel"
        )
    # Freeze treasury fuel (refunded on reject / failed apply).
    if cost_sc > 0 and author_slug:
        ok = await coin_service.treasury_debit(db, author_slug, cost_sc, f"proposal_fuel:{title}")
        if not ok:
            raise ProposalError("insufficient treasury fuel")
    p = WorldChangeProposal(
        origin=origin, origin_ref=origin_ref, author_slug=author_slug, kind=kind,
        title=title[:200], rationale_md=rationale or "", patch_json=patch or {},
        cost_sc=cost_sc, risk_level=risk_level or assess_risk(kind, patch or {}), status="pending",
        global_fencing_epoch=global_control.fencing_epoch,
    )
    db.add(p)
    if commit:
        await db.commit()
        await db.refresh(p)
    else:
        await db.flush()
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


async def reclaim_stuck_proposals(db, *, stuck_minutes: int | None = None) -> int:
    """Realism P0-5b: reclaim proposals stuck in `approved` (a crash between the
    CAS approve-commit and the applied-commit leaves them non-terminal forever —
    no recycler existed, diagnosis §2.5). Mark failed + refund fuel, reusing the
    shared `_fail_apply` path. Mirrors the Lab orphan-run reaper."""
    from app.config import settings
    minutes = settings.realism_proposal_stuck_minutes if stuck_minutes is None else stuck_minutes
    cutoff = datetime.now(UTC) - timedelta(minutes=minutes)
    rows = (await db.execute(
        select(WorldChangeProposal).where(
            WorldChangeProposal.status == "approved",
            WorldChangeProposal.approved_at.isnot(None),
            WorldChangeProposal.approved_at < cutoff,
        )
    )).scalars().all()
    n = 0
    for p in rows:
        await _fail_apply(db, p, p.review_note or "", "stuck in approved (reclaimed)")
        n += 1
    return n


async def approve_proposal(db, proposal_id: str, reviewer_id: str, note: str = "") -> WorldChangeProposal:
    p = await db.scalar(
        select(WorldChangeProposal)
        .where(WorldChangeProposal.id == proposal_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if p is None:
        raise ProposalError("proposal not found")
    if p.status != "pending":
        raise ProposalError("proposal is not pending")
    _assert_kind_enabled(p.kind)
    await _lock_apply_authority(db, p)

    # Win ownership before touching the overlay. PostgreSQL row locks already
    # serialize this path, but the explicit CAS also protects SQLite tests and
    # any caller that reaches this service without a lock-preserving wrapper.
    won = await transitions.cas_proposal_status(
        db,
        proposal_id=p.id,
        expected=("pending",),
        new="approved",
        reviewer_id=reviewer_id,
        review_note=(note or p.review_note),
    )
    if not won:
        await db.rollback()
        raise ProposalError("proposal is not pending")
    p.status = "approved"
    p.reviewer_id = reviewer_id
    p.review_note = note or p.review_note
    # Realism P0-5b: stamp approved_at in the SAME commit as the approved
    # status, so a crash before the applied commit leaves a datable "stuck
    # in approved" row the reclaim sweep can find.
    p.approved_at = datetime.now(UTC)

    location_slug = world_revision_service.location_slug_for(
        p.kind, p.patch_json or {}
    )
    base_revision_id = await world_revision_service.current_revision_id(
        db, location_slug
    )
    requested_base = (p.patch_json or {}).get("base_world_revision")
    if requested_base is not None and requested_base != base_revision_id:
        await _fail_apply(db, p, note, "stale base revision")
        raise ProposalError("stale base revision")

    try:
        before_state = await world_revision_service.capture_before_state(
            db, kind=p.kind, patch=p.patch_json or {},
        )
        await apply_engine.apply_proposal(
            db, p, broadcast=False, commit=False
        )
        after_state = await world_revision_service.capture_before_state(
            db, kind=p.kind, patch=p.patch_json or {},
        )
        revision = await world_revision_service.record_apply(
            db,
            proposal=p,
            before_state=before_state,
            after_state=after_state,
            base_revision_id=base_revision_id,
            applied_by=reviewer_id,
        )
        envelope = await world_revision_service.build_world_changed_envelope(
            db, revision=revision, action="applied", tenant_id=revision.tenant_id,
        )
        p.status = "applied"
        p.applied_at = datetime.now(UTC)
        await db.commit()
    except (apply_engine.ApplyError, world_revision_service.RevisionError) as e:
        await db.rollback()
        p = await db.get(WorldChangeProposal, proposal_id)
        await _fail_apply(db, p, note, str(e))
        raise ProposalError(f"apply failed: {e}")
    except BaseException:
        await db.rollback()
        raise

    # Reload and delivery are strictly post-commit. A fault here leaves one
    # canonical applied transaction; retry is rejected by the proposal CAS and
    # the deterministic world outbox event id cannot duplicate.
    await apply_engine.reload_world()
    await apply_engine.publish_world_reload()
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
    p = await db.scalar(
        select(WorldChangeProposal)
        .where(WorldChangeProposal.id == proposal_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if p is None:
        raise ProposalError("proposal not found")
    if p.status != "applied":
        raise ProposalError("only applied proposals can be reverted")
    _assert_kind_enabled(p.kind)
    await _lock_revert_authority(db, p)

    revision = await world_revision_service.latest_applied_revision(
        db, proposal_id=p.id, for_update=True
    )
    if revision is None:
        await db.rollback()
        raise ProposalError("applied proposal has no active world revision")

    try:
        await world_revision_service.revert_revision(db, revision=revision, reverted_by=reviewer_id)
        envelope = await world_revision_service.build_world_changed_envelope(
            db, revision=revision, action="reverted", tenant_id=revision.tenant_id,
        )
        won = await transitions.cas_proposal_status(
            db, proposal_id=p.id, expected=("applied",), new="reverted",
            reverted_at=datetime.now(UTC), reviewer_id=reviewer_id,
        )
        if not won:
            await db.rollback()
            raise ProposalError("only applied proposals can be reverted")
        await db.commit()
    except world_revision_service.RevisionError as exc:
        await db.rollback()
        raise ProposalError(f"revert failed: {exc}") from exc
    except BaseException:
        await db.rollback()
        raise

    await apply_engine.reload_world()
    await apply_engine.publish_world_reload()
    await apply_engine.broadcast_world_changed(payload=envelope)
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
