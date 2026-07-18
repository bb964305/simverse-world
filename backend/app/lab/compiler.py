"""Proposal Compiler v1 (PRD §Governance Plane): validates an Agent/Resident
-authored draft world change and, if it passes, turns it into a pending
``WorldChangeProposal`` via the existing lifecycle
(``proposal_service.create_proposal``). This is the only path a draft may take
into the world — kinds and fields outside the v1 whitelist are rejected here,
before any proposal row exists (global-constraints.md #4: no tile/collision/
path/resize/add_location/add_mechanic/NPC in v1; only ``add_lore`` and a
field-whitelisted ``edit_location``).

Draft shape: ``{"kind": ..., "patch": {...}, "title": str, "rationale": str,
"base_world_revision": str | None}``. This module never opens its own
session — ``compile_draft`` accepts ``db`` and forwards it to
``create_proposal``.
"""
from __future__ import annotations

from app.agent import map_data
from app.lab import guard
from app.services import proposal_service as psvc

ALLOWED_KINDS = ("add_lore", "edit_location")
EDIT_LOCATION_FIELDS = ("name", "description", "boosted_actions", "role")
MAX_TEXT_LEN = 2000


class CompileError(Exception):
    """Draft failed v1 validation (router maps this to 400)."""


def _validate_add_lore(patch: dict) -> tuple[str, str]:
    location_id = patch.get("location_id")
    text = patch.get("text")
    if not isinstance(location_id, str) or not location_id:
        raise CompileError("add_lore requires a non-empty location_id")
    if location_id not in map_data.LOCATIONS:
        raise CompileError(f"unknown location_id '{location_id}'")
    if not isinstance(text, str) or not (1 <= len(text) <= MAX_TEXT_LEN):
        raise CompileError(f"add_lore text must be a string of 1..{MAX_TEXT_LEN} chars")
    return location_id, text


def _validate_edit_location(patch: dict) -> tuple[str, dict]:
    location_id = patch.get("location_id")
    fields = patch.get("fields")
    if not isinstance(location_id, str) or not location_id:
        raise CompileError("edit_location requires a non-empty location_id")
    if location_id not in map_data.LOCATIONS:
        raise CompileError(f"unknown location_id '{location_id}'")
    if location_id not in map_data._dynamic_slugs:
        raise CompileError(f"edit_location target '{location_id}' is not a dynamic location")
    if not isinstance(fields, dict) or not fields:
        raise CompileError("edit_location requires a non-empty fields dict")
    extra = set(fields) - set(EDIT_LOCATION_FIELDS)
    if extra:
        raise CompileError(f"edit_location fields not allowed in v1: {sorted(extra)}")
    for key in ("name", "description", "role"):
        if key in fields and not isinstance(fields[key], str):
            raise CompileError(f"fields.{key} must be a string")
    if "boosted_actions" in fields:
        actions = fields["boosted_actions"]
        if not isinstance(actions, list) or not all(isinstance(a, str) for a in actions):
            raise CompileError("fields.boosted_actions must be a list of strings")
    return location_id, fields


async def compile_draft(
    db, *, draft: dict, origin_ref: str, author_slug: str, tenant_id: str,
) -> psvc.WorldChangeProposal:
    kind = draft.get("kind")
    if kind not in ALLOWED_KINDS:
        raise CompileError(f"kind not allowed in v1: '{kind}'")
    patch = draft.get("patch")
    if not isinstance(patch, dict):
        raise CompileError("patch must be an object")

    if kind == "add_lore":
        location_id, text = _validate_add_lore(patch)
        out_patch = {"location_id": location_id, "text": guard.redact_text(text) or ""}
    else:  # edit_location
        location_id, fields = _validate_edit_location(patch)
        clean_fields = dict(fields)
        for key in ("name", "description"):
            if key in clean_fields:
                clean_fields[key] = guard.redact_text(clean_fields[key]) or ""
        out_patch = {"slug": location_id, "data": clean_fields}

    base_world_revision = draft.get("base_world_revision")
    if base_world_revision is not None:
        out_patch["base_world_revision"] = base_world_revision

    title = guard.redact_text(str(draft.get("title") or kind)) or kind
    rationale = guard.redact_text(str(draft.get("rationale") or "")) or ""

    return await psvc.create_proposal(
        db, kind=kind, title=title, rationale=rationale, patch=out_patch,
        origin="lab_run", origin_ref=origin_ref, author_slug=author_slug, cost_sc=0,
    )
