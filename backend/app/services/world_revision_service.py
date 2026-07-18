"""Revision records + before/after-state capture for the World Governor v1
revert path (PRD §Governance Plane, 美术规格 §World Changed v1 event contract).

``WorldRevision`` (T1) is the audit trail an apply/revert pair authors: a
before/after pair captured around the overlay write is what lets
``revert_revision`` restore an ``edit_location``'s prior ``data_json`` — the
defect this module fixes (previously revert only flipped a row's
``active=False``, and ``edit_location`` doesn't even do that since
``apply.py::_apply_edit_location`` never re-attributes ``proposal_id``, so the
in-place edit was unrecoverable). ``world_changed_event`` builds the canonical
WS envelope frozen by the art spec; ``build_world_changed_envelope`` gives it a
durable, replayable ``seq`` by writing it through the existing
``OutboxEvent`` table (topic="world_changed", run_id=None) and using the row's
autoincrement id.

v1 scope: only ``add_lore`` and ``edit_location`` are revisioned
(global-constraints.md #4) — ``add_location``/``add_mechanic`` keep the
pre-T6 soft-delete revert path untouched (see ``proposal_service``).

This module never opens its own session — every function accepts ``db`` from
the caller and never commits (the caller's transaction carries the commit),
except where a flush is needed to read back an autoincrement/default id.
"""
from __future__ import annotations

import copy
import uuid
from datetime import datetime, UTC

from sqlalchemy import select

from app.agent.map_data import LOCATIONS
from app.models.dynamic_location import DynamicLocation
from app.models.dynamic_mechanic import DynamicMechanic
from app.models.lab_event import OutboxEvent
from app.models.world_revision import WorldRevision

REVISIONED_KINDS = ("add_lore", "edit_location")
SCHEMA_VERSION = 1


class RevisionError(Exception):
    """Before/after-state capture or revert-target failure (same semantics as
    ``apply.ApplyError`` for a missing target — the caller maps both to the
    proposal's "apply failed" path)."""


def location_slug_for(kind: str, patch: dict) -> str | None:
    """The location a revisioned patch targets, however the kind spells it
    (mirrors the lookups ``apply.py``'s per-kind dispatch already does)."""
    data = patch.get("data") or {}
    if kind == "edit_location":
        return patch.get("slug") or data.get("slug")
    if kind == "add_lore":
        return patch.get("location_id") or data.get("location_id")
    return None


async def current_revision_id(db, location_slug: str | None = None) -> str | None:
    """The most recent WorldRevision id (any status), globally or scoped to a
    location — the optimistic-concurrency "base" a new proposal can pin to."""
    stmt = (
        select(WorldRevision.id)
        .order_by(WorldRevision.created_at.desc(), WorldRevision.id.desc())
        .limit(1)
    )
    if location_slug is not None:
        stmt = stmt.where(WorldRevision.location_slug == location_slug)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _lore_spec(db, location_slug: str) -> dict | None:
    row = (await db.execute(
        select(DynamicMechanic).where(DynamicMechanic.code == f"lore:{location_slug}")
    )).scalar_one_or_none()
    if row is None or not row.active:
        return None
    return copy.deepcopy(row.spec_json or {})


async def _location_data(db, location_slug: str) -> dict:
    row = (await db.execute(
        select(DynamicLocation).where(DynamicLocation.slug == location_slug)
    )).scalar_one_or_none()
    if row is None:
        raise RevisionError(f"edit_location target '{location_slug}' is not a dynamic location")
    return copy.deepcopy(row.data_json or {})


async def capture_before_state(db, *, kind: str, patch: dict) -> dict | None:
    """The revision target's *current* state, matching ``kind``. Called both
    before an apply (captures the pre-image) and again after one succeeds
    (re-reading "the current state" now captures the post-image) — the same
    read, two different moments, so the two states line up field-for-field."""
    slug = location_slug_for(kind, patch)
    if kind == "add_lore":
        return await _lore_spec(db, slug) if slug else None
    if kind == "edit_location":
        if not slug:
            raise RevisionError("edit_location patch is missing a target slug")
        return await _location_data(db, slug)
    raise RevisionError(f"revision capture not supported for kind '{kind}'")


async def record_apply(
    db, *, proposal, before_state: dict | None, after_state: dict | None,
    base_revision_id: str | None, applied_by: str | None,
) -> WorldRevision:
    """Add (not commit) a WorldRevision row for a just-applied proposal. The
    caller commits it together with the proposal's status flip so the two are
    atomic (self-review checkpoint: revision+status="applied" together)."""
    location_slug = location_slug_for(proposal.kind, proposal.patch_json or {})
    # World-governance tenant_id is author attribution (author_slug), not the
    # lab-run tenant_id (issuer_user_id) used elsewhere in app/lab/* — the two
    # are intentionally different scopes.
    revision = WorldRevision(
        tenant_id=proposal.author_slug or "system",
        proposal_id=proposal.id,
        location_slug=location_slug or "",
        change_kind=proposal.kind,
        base_revision_id=base_revision_id,
        before_state_json=before_state,
        after_state_json=after_state,
        status="applied",
        applied_by=applied_by,
    )
    db.add(revision)
    await db.flush()  # populate revision.id for the caller's envelope build
    return revision


async def latest_applied_revision(db, *, proposal_id: str) -> WorldRevision | None:
    """The most recent still-``applied`` revision this proposal authored, or
    None if it never got one (e.g. a legacy proposal predating T6)."""
    stmt = (
        select(WorldRevision)
        .where(WorldRevision.proposal_id == proposal_id, WorldRevision.status == "applied")
        .order_by(WorldRevision.created_at.desc(), WorldRevision.id.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def revert_revision(db, *, revision: WorldRevision, reverted_by: str | None) -> None:
    """Restore the overlay row to ``revision.before_state_json`` and flip the
    revision to reverted. Does not commit, reload the world, or broadcast —
    the caller (``proposal_service.revert_proposal``) owns that sequencing."""
    if revision.change_kind == "add_lore":
        row = (await db.execute(
            select(DynamicMechanic).where(DynamicMechanic.code == f"lore:{revision.location_slug}")
        )).scalar_one_or_none()
        if row is not None:
            if revision.before_state_json is None:
                row.active = False
            else:
                row.spec_json = dict(revision.before_state_json)
                row.active = True
    elif revision.change_kind == "edit_location":
        row = (await db.execute(
            select(DynamicLocation).where(DynamicLocation.slug == revision.location_slug)
        )).scalar_one_or_none()
        if row is None:
            raise RevisionError(f"revert target '{revision.location_slug}' no longer exists")
        row.data_json = dict(revision.before_state_json or {})
    else:
        raise RevisionError(f"revert not supported for change_kind '{revision.change_kind}'")
    revision.status = "reverted"
    revision.reverted_by = reverted_by
    revision.reverted_at = datetime.now(UTC)


def world_changed_event(*, revision: WorldRevision, action: str, seq: int, event_id: str, occurred_at: datetime) -> dict:
    """The frozen ``world_changed`` v1 envelope (美术规格 §World Changed v1),
    logged verbatim so a WS/replay consumer never has to guess a field."""
    bounds = LOCATIONS.get(revision.location_slug, {}).get("bounds")
    return {
        "type": "world_changed",
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id,
        "tenant_id": revision.tenant_id,
        "seq": seq,
        "world_revision_id": revision.id,
        "proposal_id": revision.proposal_id,
        "location_slug": revision.location_slug,
        "action": action,
        "change_kind": revision.change_kind,
        "bounds": list(bounds) if bounds else None,
        "occurred_at": occurred_at.isoformat(),
    }


async def build_world_changed_envelope(db, *, revision: WorldRevision, action: str, tenant_id: str) -> dict:
    """Write a durable outbox row (topic="world_changed", run_id=None) and
    return the canonical envelope, using the row's autoincrement id as
    ``seq`` — reusing ``OutboxEvent`` gives a durable, replayable seq for free
    (v1 decision, task-6-brief.md). The row's ``id`` isn't known until it's
    flushed, so it's inserted with a placeholder payload first, then updated
    in place — both statements land in the caller's transaction (not
    committed here)."""
    event_id = str(uuid.uuid4())
    outbox = OutboxEvent(
        event_id=event_id, tenant_id=tenant_id, run_id=None,
        topic="world_changed", payload_json={},
    )
    db.add(outbox)
    await db.flush()
    occurred_at = datetime.now(UTC)
    envelope = world_changed_event(
        revision=revision, action=action, seq=outbox.id,
        event_id=event_id, occurred_at=occurred_at,
    )
    outbox.payload_json = envelope
    return envelope
