"""Lab resource ACL — pure predicates + the server-authoritative approval
projection (PRD §Instruction and Memory Layers tenant ACL, §V03). Tenant
mapping (v1, per global constraints): a lab record's tenant is its task's
``issuer_user_id`` — the task owner is the sole non-admin reader/decider.
A run's assigned researcher/resident is an executor, not a tenant, and gains
no read access by that role alone.

No DB access here — callers own the session and pass in already-loaded rows.
A denial is ``AclDenied``; routers must translate it to 404, never 403, so a
cross-tenant probe can't distinguish "exists but isn't yours" from "doesn't
exist".
"""
from __future__ import annotations

from datetime import datetime, UTC


class AclDenied(Exception):
    """Caller lacks access to this lab resource."""


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands datetimes back naive; treat a stored value as UTC so it
    can be compared against an aware ``now`` without a TypeError."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def can_read_task(task, *, user_id: str, is_admin: bool) -> bool:
    return is_admin or task.issuer_user_id == user_id


def can_read_run(run, task, *, user_id: str, is_admin: bool) -> bool:
    # Ownership flows from the task, not the run — a run's resident/creator
    # gains no read access by having executed it.
    return can_read_task(task, user_id=user_id, is_admin=is_admin)


def can_decide_approval(approval, task, *, user_id: str, is_admin: bool) -> bool:
    return can_read_task(task, user_id=user_id, is_admin=is_admin)


def approval_projection(approval, task, *, user_id: str, is_admin: bool) -> dict:
    """Server-authoritative shape the client gates its approve/deny controls
    on. A hard-denied action never has an approval row to project, so it is
    naturally always ``[]`` here — nothing special to encode for it."""
    can_decide = can_decide_approval(approval, task, user_id=user_id, is_admin=is_admin)
    pending = approval.decision == "pending"
    not_expired = _aware(approval.expires_at) > datetime.now(UTC)
    allowed_actions = ["approve", "deny"] if (can_decide and pending and not_expired) else []
    return {
        "allowed_actions": allowed_actions,
        "can_decide": can_decide,
        "decision_scope": approval.decision_scope,
        "status": approval.decision,
    }
