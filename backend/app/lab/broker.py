"""Tool Broker — the single enforcement point for every brokered effect (PRD
§Tool Contract, §Capability and Approval Model, authoritative sequence 1-7).

Thin D architecture: the runtime only *intends* a tool call; nothing crosses to
a real effect except through this module. A cooperative runtime is not evidence
of safety, so the Broker re-derives every gate here — it verifies the grant is
still active, re-runs the Policy Engine immediately before execution, screens
egress targets, and consumes a one-shot approval atomically. The subtle,
load-bearing invariants:

* A hard deny (unknown tool / R4 / unregistered-financial) never writes a
  ``LabApproval`` row; a forged approval therefore has nothing to consume and
  is caught anyway because policy is re-evaluated before execution.
* An approval binds ``action_id`` + args digest + expiry. Any change to the
  args (digest mismatch) or a lapsed window invalidates it.
* Consumption is a single conditional ``UPDATE ... WHERE decision='approved'
  AND consumed_at IS NULL AND expires_at > now`` — the row-count check is what
  makes concurrent double-execution impossible.
* Every denial leaves an append-only audit row (``status='denied'``); stored
  args and results are redacted first.

This module owns the action lifecycle only. It never opens its own session
(callers own the transaction boundary), never duplicates policy logic (it calls
``policy.decide``), and never signs grants (it calls ``grants``).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, UTC
from urllib.parse import urlparse

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.lab import grants, guard, policy, protocol
from app.lab.sandbox import isolation
from app.models.lab_action import LabToolAction, LabApproval

# Terminal / in-flight states: re-invoking execute_action on one of these must
# never run the executor again (idempotent, at-least-once delivery safety).
_NO_REEXEC = ("succeeded", "failed", "executing", "reconciliation_required")


class BrokerError(Exception):
    """Base for every Broker-level failure."""


class ActionDenied(BrokerError):
    """Policy/grant/egress refused the call. Carries the ``PolicyDecision`` when
    one exists and the persisted (denied) audit ``action`` row."""

    def __init__(self, reason, *, decision=None, action=None, hard=False):
        super().__init__(reason)
        self.reason = reason
        self.decision = decision
        self.action = action
        self.hard = hard


class ApprovalRequired(BrokerError):
    """Execution attempted while the gating approval is still pending."""

    def __init__(self, *, action_id=None, approval_id=None):
        super().__init__("approval required")
        self.action_id = action_id
        self.approval_id = approval_id


class ApprovalInvalid(BrokerError):
    """The approval cannot authorise this execution: wrong actor, expired,
    digest mismatch, already consumed, or otherwise not consumable."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class UncertainOutcome(Exception):
    """An executor raises this when a side effect may have partially happened
    and cannot be confirmed — the action is parked for reconciliation rather
    than retried."""


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite hands datetimes back naive; treat a stored value as UTC so it can
    be compared against an aware ``now`` without a TypeError."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


async def _emit(on_event, obj) -> None:
    """Reserved hook for the T4 event ledger. No-op until wired by T7."""
    if on_event is not None:
        await on_event(obj)


def _reason_of(action: LabToolAction) -> str:
    if isinstance(action.result_json, dict):
        return action.result_json.get("reason", "denied")
    return "denied"


def _validate_egress(tool, args: dict, claims) -> tuple[str, bool] | None:
    """Screen a tool call's egress target. Returns ``(reason, hard)`` to deny,
    or ``None`` to allow. Reuses ``sandbox.isolation`` — no rewritten matching.

    * A literal internal/metadata IP is blocked outright (anti-SSRF), even under
      a wildcard grant.
    * A network tool (http/browse) carrying a url may only reach a host inside
      ``claims.egress``; an empty egress list is fail-closed (default no net).
    """
    if tool is None:
        return None
    raw = args.get("url") or args.get("target")
    if not raw:
        return None
    url = str(raw)
    host = (urlparse(url).hostname or "").lower()
    if host and isolation.is_host_blocked(host):
        return ("egress_blocked_host", True)
    if tool.capability in ("http", "browse"):
        if not claims.egress:
            return ("egress_not_granted", False)
        if not isolation.is_egress_allowed(url, claims.egress):
            return ("egress_not_granted", False)
    return None


def _preview(action_id: str, tool, args: dict, digest: str, claims, expires_at: datetime) -> dict:
    target = ""
    for key in ("url", "target", "query", "path", "prompt", "command"):
        val = args.get(key)
        if val:
            target = str(guard.redact_text(str(val)))
            break
    preview = protocol.ApprovalPreview(
        action_id=action_id,
        tool_name=tool.name,
        target=target,
        side_effect=("mutating" if tool.side_effect else "read-only"),
        cost_summary="",
        expires_at=expires_at,
        args_digest=digest,
        actor=claims.agent_id,
    )
    return preview.model_dump(mode="json")


async def _by_idem(db, idempotency_key: str) -> LabToolAction | None:
    res = await db.execute(
        select(LabToolAction).where(LabToolAction.idempotency_key == idempotency_key)
    )
    return res.scalar_one_or_none()


async def _persist(db, action: LabToolAction, approval: LabApproval | None = None):
    """Commit a new action (and optionally its approval) as one transaction.
    On a unique-key collision (concurrent duplicate request) re-read and return
    the winner — at-least-once request semantics."""
    db.add(action)
    if approval is not None:
        db.add(approval)
    try:
        await db.commit()
        return action, False
    except IntegrityError:
        await db.rollback()
        existing = await _by_idem(db, action.idempotency_key)
        if existing is not None:
            return existing, True
        raise


async def request_action(db, *, claims, token, tool_name, args, idempotency_key=None,
                         expected_epoch=None, on_event=None) -> LabToolAction:
    """Request → policy → (approval) → persisted action, all audited.

    Returns the action in ``approved`` (allow passthrough) or
    ``waiting_approval`` (ask) state; raises ``ActionDenied`` on any refusal,
    always leaving a ``denied`` audit row and never an approval row.
    """
    now = _now()
    digest = protocol.args_digest(args)
    action_id = str(uuid.uuid4())
    idem = idempotency_key or action_id

    # Idempotency: a duplicate key returns the already-processed action; the
    # executor is never re-run and no second row is created.
    existing = await _by_idem(db, idem)
    if existing is not None:
        return existing

    redacted_args = guard.redact_payload(args)

    def _build(status, risk_class, *, approval_id=None, result_json=None):
        return LabToolAction(
            id=action_id, tenant_id=claims.tenant_id, run_id=claims.run_id,
            task_id=claims.task_id, tool_name=tool_name, args_hash=digest,
            args_redacted_json=redacted_args, risk_class=risk_class, status=status,
            grant_jti=claims.jti, fencing_epoch=claims.fencing_epoch,
            policy_version=claims.policy_version, idempotency_key=idem,
            approval_id=approval_id, result_json=result_json,
        )

    async def _deny(risk_class, reason, *, hard, decision=None):
        action = _build("denied", risk_class, result_json={"reason": reason})
        stored, existed = await _persist(db, action)
        if not existed:
            await _emit(on_event, stored)
        raise ActionDenied(reason, decision=decision, action=stored, hard=hard)

    # 2. Grant: verify the presented token, bind it to these claims, and confirm
    # it is still active (not revoked / stale epoch). Any failure is a denial.
    try:
        verified = grants.verify_grant(token)
        if verified.jti != claims.jti:
            raise grants.GrantError("grant/claims jti mismatch")
        await grants.check_grant_active(db, claims, expected_epoch=expected_epoch)
    except grants.GrantError as exc:
        await _deny("R4", f"grant_inactive: {exc}", hard=True)

    # 3. Policy: deny > ask > allow. Hard denies and governance routes never
    # create an approval row.
    decision = policy.decide(tool_name, args, claims)
    if decision.effect == "deny":
        await _deny(decision.risk_class, decision.reason, hard=decision.hard_deny, decision=decision)
    if decision.effect == "govern":
        await _deny(decision.risk_class, "governance_route", hard=False, decision=decision)

    # 3b. Egress target screening (allow/ask only).
    egress = _validate_egress(decision.tool, args, claims)
    if egress is not None:
        reason, hard = egress
        await _deny(decision.risk_class, reason, hard=hard, decision=decision)

    # 4. ask → waiting_approval + a pending approval, committed together.
    if decision.effect == "ask":
        approval_id = str(uuid.uuid4())
        expires_at = now + timedelta(seconds=settings.lab_approval_timeout_s)
        approval = LabApproval(
            id=approval_id, tenant_id=claims.tenant_id, run_id=claims.run_id,
            task_id=claims.task_id, action_id=action_id, args_digest=digest,
            decision="pending", expires_at=expires_at, fencing_epoch=claims.fencing_epoch,
            preview_json=_preview(action_id, decision.tool, args, digest, claims, expires_at),
        )
        action = _build("waiting_approval", decision.risk_class, approval_id=approval_id)
        stored, existed = await _persist(db, action, approval)
        if not existed:
            await _emit(on_event, stored)
        return stored

    # 5. allow → approved passthrough (no approval row).
    action = _build("approved", decision.risk_class)
    stored, existed = await _persist(db, action)
    if not existed:
        await _emit(on_event, stored)
    return stored


async def decide_approval(db, *, approval_id, decider_user_id, approve, task_owner_id,
                          is_admin=False) -> LabApproval:
    """Resolve a pending approval. Only the task owner (or an admin) may decide;
    the window must not have lapsed; a decided/consumed approval cannot be
    re-decided. Writes the decision and flips the action status in one txn."""
    now = _now()
    approval = await db.get(LabApproval, approval_id)
    if approval is None:
        raise ApprovalInvalid("not_found")

    # 5. Actor binding: approval controls are server-side, never widened by the
    # caller's role. Owner or admin only.
    if not (is_admin or decider_user_id == task_owner_id):
        raise ApprovalInvalid("actor")

    if approval.consumed_at is not None:
        raise ApprovalInvalid("already_consumed")
    if approval.decision != "pending":
        raise ApprovalInvalid(f"already_{approval.decision}")

    if _aware(approval.expires_at) <= now:
        approval.decision = "expired"
        await db.commit()
        raise ApprovalInvalid("expired")

    approval.decision = "approved" if approve else "denied"
    approval.decided_by = decider_user_id
    approval.decided_at = now
    action = await db.get(LabToolAction, approval.action_id)
    if action is not None:
        action.status = "approved" if approve else "denied"
    await db.commit()
    return approval


async def execute_action(db, *, action_id, claims, executor, args, expected_epoch=None,
                         on_event=None) -> LabToolAction:
    """Execute a previously-requested action through ``executor(tool_name, args)``.

    Gates, in order: idempotent short-circuit on terminal states; digest binding;
    re-evaluated policy + active grant + egress (a forged approval dies here);
    atomic one-shot approval consumption; then the executor with the
    ``executing`` intent committed first so a crash is reconcilable.
    """
    now = _now()
    action = await db.get(LabToolAction, action_id)
    if action is None:
        raise BrokerError(f"unknown action {action_id}")

    # Idempotent short-circuit — never re-run a terminal/in-flight action.
    if action.status in _NO_REEXEC:
        return action
    if action.status == "denied":
        raise ActionDenied(_reason_of(action), action=action)
    if action.status == "cancelled":
        raise ApprovalInvalid("cancelled")

    # Digest binding: the approved args must be exactly the args presented now.
    if protocol.args_digest(args) != action.args_hash:
        raise ApprovalInvalid("digest_mismatch")

    # Re-evaluate the grant + policy right before execution (Thin D: the runtime
    # is not trusted). A denial parks the action as denied and refuses.
    async def _deny_now(reason, *, decision=None, hard=False):
        action.status = "denied"
        action.result_json = {"reason": reason}
        await db.commit()
        await _emit(on_event, action)
        raise ActionDenied(reason, decision=decision, action=action, hard=hard)

    try:
        await grants.check_grant_active(db, claims, expected_epoch=expected_epoch)
    except grants.GrantError as exc:
        await _deny_now(f"grant_inactive: {exc}", hard=True)

    decision = policy.decide(action.tool_name, args, claims)
    if decision.effect == "deny":
        await _deny_now(decision.reason, decision=decision, hard=decision.hard_deny)
    if decision.effect == "govern":
        await _deny_now("governance_route", decision=decision)

    egress = _validate_egress(decision.tool, args, claims)
    if egress is not None:
        reason, hard = egress
        await _deny_now(reason, decision=decision, hard=hard)

    # Atomic one-shot consumption of the gating approval (if any).
    if action.approval_id is not None:
        approval = await db.get(LabApproval, action.approval_id)
        if approval is None:
            raise ApprovalInvalid("missing_approval")
        if approval.decision == "denied":
            raise ActionDenied("approval_denied", action=action)
        if approval.decision == "pending":
            raise ApprovalRequired(action_id=action.id, approval_id=approval.id)
        if approval.decision == "expired":
            raise ApprovalInvalid("expired")
        # decision == "approved": the conditional UPDATE + rowcount check is the
        # real gate — exactly one caller can flip consumed_at from NULL.
        stmt = (
            update(LabApproval)
            .where(
                LabApproval.id == action.approval_id,
                LabApproval.decision == "approved",
                LabApproval.consumed_at.is_(None),
                LabApproval.expires_at > now,
            )
            .values(consumed_at=now)
            .execution_options(synchronize_session=False)
        )
        result = await db.execute(stmt)
        if result.rowcount != 1:
            await db.rollback()
            raise ApprovalInvalid("not_consumable")

    # Status gate: only an approved action executes.
    if action.status != "approved":
        if action.status == "waiting_approval":
            raise ApprovalRequired(action_id=action.id, approval_id=action.approval_id)
        raise ApprovalInvalid(f"not_executable:{action.status}")

    # 7. Commit the executing intent (and the consume, same txn) before the side
    # effect, so a mid-flight crash leaves a durable reconciliation trail.
    action.status = "executing"
    action.attempts = (action.attempts or 0) + 1
    await db.commit()
    await _emit(on_event, action)

    try:
        result = await executor(action.tool_name, args)
    except UncertainOutcome as exc:
        action.status = "reconciliation_required"
        action.result_json = {"error": guard.redact_text(str(exc)), "uncertain": True}
        await db.commit()
        await _emit(on_event, action)
        return action
    except Exception as exc:  # includes protocol.ProtocolError
        action.status = "failed"
        action.result_json = {"error": guard.redact_text(str(exc))}
        await db.commit()
        await _emit(on_event, action)
        return action

    action.status = "succeeded"
    if isinstance(result, (dict, list)):
        action.result_json = guard.redact_payload(result)
    else:
        action.result_json = {"result": guard.redact_payload(result)}
    await db.commit()
    await _emit(on_event, action)
    return action
