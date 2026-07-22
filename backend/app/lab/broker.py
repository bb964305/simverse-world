"""Tool Broker — the single enforcement point for every brokered effect (PRD
§Tool Contract, §Capability and Approval Model, authoritative sequence 1-7).

Thin D architecture: the runtime only *intends* a tool call; nothing crosses to
a real effect except through this module. A cooperative runtime is not evidence
of safety, so the Broker re-derives every gate here — it verifies the grant is
still active, reconciles the caller's fencing epoch against the run-lease
authority (a taken-over owner is denied even though its own cached epoch still
matches its token), re-runs the Policy Engine immediately before execution,
screens egress targets, and consumes a one-shot approval atomically. The subtle,
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

from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.lab import budgets, grants, guard, leases, policy, protocol, telemetry
from app.lab.sandbox import isolation
from app.models.lab_action import LabToolAction, LabApproval
from app.models.lab_runtime import (
    LabRuntimeIntent,
    LabRuntimeResult,
    LabRuntimeSession,
    LabRuntimeTurn,
)

# Terminal / parked states: re-invoking execute_action on one of these must
# never run the executor again — return the existing outcome (idempotent,
# at-least-once delivery safety). ``executing`` is deliberately NOT here: an
# in-flight action is guarded by the atomic approved->executing claim below, so
# a lost race raises rather than silently returning a half-finished action.
_NO_REEXEC = ("succeeded", "failed", "reconciliation_required")


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


class RuntimeResultConflict(BrokerError):
    """A Broker outcome cannot be bound to the requested Runtime intent."""


def _runtime_outcome(action: LabToolAction) -> tuple[str, dict]:
    value = guard.redact_payload(action.result_json or {})
    if not isinstance(value, dict):
        value = {"result": value}
    if action.status == "succeeded":
        return "succeeded", value
    if action.status == "denied":
        return "denied", value
    if action.status in {
        "failed",
        "cancelled",
        "reconciliation_required",
    }:
        return "failed", value
    raise RuntimeResultConflict(
        f"Broker action {action.id} has no terminal outcome: {action.status}"
    )


async def _persist_runtime_result(
    db,
    *,
    session_id: str,
    intent_row_id: str,
    action: LabToolAction,
    owner_id: str,
) -> protocol.ToolResultCommand:
    """Persist one ToolResultCommand before any Runtime delivery attempt.

    The delivery receipt remains NULL until the authenticated Runtime response
    is validated and CASed by supervision. This makes the pre-delivery row an
    honest durable ToolResultCommand, not a fabricated receipt.
    """
    from app.lab import supervision

    runtime_epoch = await db.scalar(
        select(LabRuntimeSession.fencing_epoch).where(
            LabRuntimeSession.id == session_id
        )
    )
    if runtime_epoch is None:
        raise RuntimeResultConflict("Runtime session binding is missing")
    session = await supervision.lock_runtime_authority(
        db,
        run_id=action.run_id,
        session_id=session_id,
        epoch=runtime_epoch,
        owner_id=owner_id,
        provider_binding=False,
    )
    intent = await db.scalar(
        select(LabRuntimeIntent)
        .where(LabRuntimeIntent.id == intent_row_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if intent is None or intent.session_id != session_id:
        raise RuntimeResultConflict("Runtime session or intent binding is missing")
    turn = await db.scalar(
        select(LabRuntimeTurn)
        .where(
            LabRuntimeTurn.id == intent.runtime_turn_id,
            LabRuntimeTurn.session_id == session_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if turn is None:
        raise RuntimeResultConflict("Runtime turn binding is missing")
    if session.status != "ready" or session.protocol_version != protocol.PROTOCOL_V2:
        raise RuntimeResultConflict(
            f"Runtime result cannot be persisted in session state {session.status}"
        )
    if not session.provider_session_id:
        raise RuntimeResultConflict("Runtime provider session binding is missing")
    if (
        action.run_id != session.run_id
        or action.status not in _NO_REEXEC + ("denied",)
        or not session.fencing_epoch
        <= action.fencing_epoch
        <= session.authority_epoch
        or action.tool_name != intent.tool_name
        or action.args_hash != intent.args_digest
    ):
        raise RuntimeResultConflict("Broker action changed the Runtime intent binding")
    if intent.action_id not in (None, action.id):
        raise RuntimeResultConflict("Runtime intent is already bound to another action")

    outcome, payload = _runtime_outcome(action)
    result_digest = protocol.content_digest(payload)
    command_id = str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            "simverse:lab-runtime-result:"
            f"{session.id}:{intent.intent_id}:{action.id}:{result_digest}",
        )
    )
    try:
        command = protocol.ToolResultCommand(
            command_id=command_id,
            run_id=session.run_id,
            session_id=session.provider_session_id,
            turn_id=turn.turn_id,
            intent_id=intent.intent_id,
            action_id=action.id,
            outcome=outcome,
            payload=payload,
            result_digest=result_digest,
            epoch=session.fencing_epoch,
        )
    except ValidationError as exc:
        raise RuntimeResultConflict(
            "Broker result cannot be encoded as a bounded Runtime command"
        ) from exc
    request_digest = protocol.content_digest(command.model_dump(mode="json"))
    existing = await db.scalar(
        select(LabRuntimeResult).where(
            LabRuntimeResult.runtime_intent_id == intent.id
        )
    )
    if existing is not None:
        if (
            existing.command_id != command.command_id
            or existing.session_id != session.id
            or existing.runtime_turn_id != turn.id
            or existing.intent_id != command.intent_id
            or existing.action_id != command.action_id
            or existing.outcome != command.outcome
            or existing.request_digest != request_digest
            or existing.result_digest != command.result_digest
            or existing.payload_json != command.payload
            or existing.fencing_epoch != command.epoch
        ):
            raise RuntimeResultConflict(
                "Runtime intent already has a different Broker result"
            )
        await supervision.assert_runtime_authority_live(
            db, session=session, owner_id=owner_id
        )
        await db.commit()
        return command

    if intent.status != "pending" or turn.status != "intent_pending":
        raise RuntimeResultConflict("Runtime intent cannot record a result now")
    intent.action_id = action.id
    intent.status = "result_recorded"
    turn.status = "result_recorded"
    await db.flush()
    db.add(
        LabRuntimeResult(
            session_id=session.id,
            runtime_turn_id=turn.id,
            runtime_intent_id=intent.id,
            intent_id=intent.intent_id,
            action_id=action.id,
            command_id=command.command_id,
            receipt_id=None,
            outcome=command.outcome,
            request_digest=request_digest,
            result_digest=command.result_digest,
            payload_json=command.payload,
            fencing_epoch=command.epoch,
        )
    )
    await supervision.assert_runtime_authority_live(
        db, session=session, owner_id=owner_id
    )
    await db.commit()
    return command


async def persist_runtime_result(
    db,
    *,
    session_id: str,
    intent_row_id: str,
    action: LabToolAction,
    owner_id: str,
) -> protocol.ToolResultCommand:
    try:
        return await _persist_runtime_result(
            db,
            session_id=session_id,
            intent_row_id=intent_row_id,
            action=action,
            owner_id=owner_id,
        )
    except BaseException:
        await db.rollback()
        raise


async def pending_runtime_result_commands(
    db, *, session_id: str
) -> list[protocol.ToolResultCommand]:
    """Rebuild every persisted-but-unacknowledged Runtime command exactly."""
    session = await db.get(LabRuntimeSession, session_id)
    if session is None or not session.provider_session_id:
        raise RuntimeResultConflict("Runtime provider session binding is missing")
    rows = (
        await db.execute(
            select(LabRuntimeResult)
            .where(
                LabRuntimeResult.session_id == session_id,
                LabRuntimeResult.runtime_acked_at.is_(None),
            )
            .order_by(LabRuntimeResult.created_at, LabRuntimeResult.id)
        )
    ).scalars().all()
    commands: list[protocol.ToolResultCommand] = []
    for row in rows:
        turn = await db.get(LabRuntimeTurn, row.runtime_turn_id)
        if turn is None or turn.session_id != session_id:
            raise RuntimeResultConflict("Runtime result turn binding is missing")
        command = protocol.ToolResultCommand(
            command_id=row.command_id,
            run_id=session.run_id,
            session_id=session.provider_session_id,
            turn_id=turn.turn_id,
            intent_id=row.intent_id,
            action_id=row.action_id,
            outcome=row.outcome,
            payload=row.payload_json,
            result_digest=row.result_digest,
            epoch=row.fencing_epoch,
        )
        if row.request_digest != protocol.content_digest(
            command.model_dump(mode="json")
        ):
            raise RuntimeResultConflict("Runtime result request digest changed")
        commands.append(command)
    return commands


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


def _is_egress_action(tool, args: dict) -> bool:
    """True if executing this call reaches the network — an http/browse-capability
    tool carrying a ``url``/``target``. The egress budgets (``egress_requests`` /
    ``egress_bytes``) accrue only for these; a web.search (no network target) or a
    local tool never touches them. Keyed on capability + target so it matches the
    same actions ``_validate_egress`` screens for the allowlist."""
    if tool is None or getattr(tool, "capability", None) not in ("http", "browse"):
        return False
    return bool(args.get("url") or args.get("target"))


def is_egress_action(tool_name: str, args: dict) -> bool:
    """``_is_egress_action`` for a caller that only has the tool *name* (the
    orchestrator, settling reservations on its release paths). Resolves the tool
    through the same policy registry the Broker's own ``decision.tool`` comes
    from, so both sides agree on which actions carry egress reservations."""
    return _is_egress_action(policy.TOOL_REGISTRY.get(tool_name), args)


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
    idempotency_key = action.idempotency_key
    db.add(action)
    if approval is not None:
        db.add(approval)
    try:
        await db.commit()
        return action, False
    except IntegrityError:
        await db.rollback()
        existing = await _by_idem(db, idempotency_key)
        if existing is not None:
            return existing, True
        raise
    except BaseException:
        await db.rollback()
        raise


async def request_action(db, *, claims, token, tool_name, args, idempotency_key=None,
                         expected_epoch=None, on_event=None) -> LabToolAction:
    """Request → policy → (approval) → persisted action, all audited.

    Returns the action in ``approved`` (allow passthrough) or
    ``waiting_approval`` (ask) state; raises ``ActionDenied`` on any refusal,
    always leaving a ``denied`` audit row and never an approval row.

    Idempotency asymmetry (intentional): the *first* call for a given
    ``idempotency_key`` raises ``ActionDenied`` on a refusal, but a *replay* of
    that key takes the short-circuit below and **returns** the stored action —
    which may itself be ``status == "denied"`` — rather than re-raising. Callers
    that replay a key must therefore inspect ``action.status`` instead of relying
    on an exception to signal a denial.
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
        # Content-free security alerts, keyed on the fixed reason CODE only.
        if reason == "stale_epoch":
            telemetry.emit_alert(telemetry.LabAlert.STALE_EPOCH, run_id=claims.run_id,
                                 tenant_id=claims.tenant_id, epoch=claims.fencing_epoch)
        elif reason in ("egress_not_granted", "egress_blocked_host"):
            telemetry.emit_alert(telemetry.LabAlert.BLOCKED_EGRESS, run_id=claims.run_id,
                                 tenant_id=claims.tenant_id, reason=reason)
        raise ActionDenied(reason, decision=decision, action=stored, hard=hard)

    # 2. Grant: verify the presented token, bind it to these claims, and confirm
    # it is still active (not revoked / stale epoch). Any failure is a denial.
    try:
        verified = grants.verify_grant(token)
        if verified.jti != claims.jti:
            raise grants.GrantError("grant/claims jti mismatch")
        await grants.check_grant_active(db, claims, expected_epoch=expected_epoch)
    except grants.GrantError as exc:
        # "NA": a lifecycle/grant-expiry denial, not a tool-risk one — tagging it
        # R4 would misattribute it to attack-surface stats keyed on risk_class.
        await _deny("NA", f"grant_inactive: {exc}", hard=True)

    # 2b. Lease reconciliation (structural fencing). The grant's epoch is only
    # trustworthy if it still matches the lab_run_leases row — the authority. The
    # P1 ``expected_epoch`` gate compared the caller's own cached epoch against
    # the token's, which a stale owner passes (both are its stale epoch).
    # Reconciling ``claims.fencing_epoch`` against the live lease closes that
    # self-reference: a taken-over owner is denied here. A run with no lease row
    # is epoch 0, so a zero-epoch grant passes — every existing test that never
    # creates a lease is unaffected. This is additive to ``expected_epoch``.
    try:
        await leases.assert_epoch(db, run_id=claims.run_id, epoch=claims.fencing_epoch)
    except leases.StaleEpoch:
        await _deny("NA", "stale_epoch", hard=True)

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

    # 3c. Stage both request-side reservations together. ``_persist`` commits
    # them with the action/approval, so partial egress reservation, persistence
    # failure, and idempotency-key races cannot orphan a counter.
    is_egress = _is_egress_action(decision.tool, args)
    exhausted = await budgets.stage_action_reservation(
        db, run_id=claims.run_id, is_egress=is_egress
    )
    if exhausted is not None:
        action = _build(
            "denied",
            decision.risk_class,
            result_json={"reason": f"budget_exhausted:{exhausted}"},
        )
        stored, existed = await _persist(db, action)
        if existed:
            return stored
        if not existed:
            await _emit(on_event, stored)
        await grants.revoke_run_grants(db, claims.run_id)
        telemetry.emit_alert(
            telemetry.LabAlert.BUDGET_EXHAUSTED,
            run_id=claims.run_id,
            tenant_id=claims.tenant_id,
            dimension=exhausted,
            reason="limit",
        )
        raise budgets.BudgetExhausted(exhausted)

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


async def recover_action_authority(
    db,
    *,
    action_id: str,
    claims,
    token: str,
    tool_name: str,
    args: dict,
    expected_epoch: int,
) -> LabToolAction:
    """Adopt only a pre-effect replayable action after Gateway takeover."""
    verified = grants.verify_grant(token)
    if verified.jti != claims.jti or claims.fencing_epoch != expected_epoch:
        raise RuntimeResultConflict("recovery grant binding changed")
    await grants.check_grant_active(db, claims, expected_epoch=expected_epoch)
    await leases.assert_epoch(
        db, run_id=claims.run_id, epoch=expected_epoch
    )
    action = await db.scalar(
        select(LabToolAction)
        .where(LabToolAction.id == action_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        action is None
        or action.run_id != claims.run_id
        or action.task_id != claims.task_id
        or action.tenant_id != claims.tenant_id
        or action.tool_name != tool_name
        or action.args_hash != protocol.args_digest(args)
        or action.fencing_epoch > expected_epoch
    ):
        raise RuntimeResultConflict("replayed Broker action binding changed")
    if action.fencing_epoch == expected_epoch or action.status in _NO_REEXEC + (
        "denied",
    ):
        await db.commit()
        return action
    if action.status == "executing":
        raise RuntimeResultConflict(
            "replayed Broker action has an uncertain executing effect"
        )
    if action.status not in {"approved", "waiting_approval"}:
        raise RuntimeResultConflict(
            f"replayed Broker action cannot transfer from state {action.status}"
        )
    if action.attempts:
        raise RuntimeResultConflict(
            "replayed Broker action has a nonzero pre-effect attempt count"
        )
    if action.approval_id is not None:
        approval = await db.scalar(
            select(LabApproval)
            .where(LabApproval.id == action.approval_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            approval is None
            or approval.action_id != action.id
            or approval.fencing_epoch != action.fencing_epoch
            or approval.decision not in {"pending", "approved"}
        ):
            raise RuntimeResultConflict(
                "replayed Broker approval binding changed"
            )
        approval.fencing_epoch = expected_epoch
    action.fencing_epoch = expected_epoch
    action.grant_jti = claims.jti
    await leases.assert_epoch(
        db, run_id=claims.run_id, epoch=expected_epoch
    )
    await db.commit()
    return action


async def _release_action_reservations(db, action: LabToolAction) -> None:
    await budgets.stage_action_settlement(
        db,
        run_id=action.run_id,
        succeeded=False,
        is_egress=is_egress_action(
            action.tool_name, dict(action.args_redacted_json or {})
        ),
    )


async def _decide_approval(db, *, approval_id, decider_user_id, approve, task_owner_id,
                           is_admin=False) -> LabApproval:
    """Resolve a pending approval. Only the task owner (or an admin) may decide;
    the window must not have lapsed; a decided/consumed approval cannot be
    re-decided. Writes the decision and flips the action status in one txn."""
    now = _now()
    approval = await db.scalar(
        select(LabApproval)
        .where(LabApproval.id == approval_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
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

    action = await db.scalar(
        select(LabToolAction)
        .where(LabToolAction.id == approval.action_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        action is None
        or action.approval_id != approval.id
        or action.run_id != approval.run_id
        or action.task_id != approval.task_id
        or action.fencing_epoch != approval.fencing_epoch
        or action.status != "waiting_approval"
    ):
        raise ApprovalInvalid("approval_binding")

    if _aware(approval.expires_at) <= now:
        try:
            approval.decision = "expired"
            approval.decided_at = now
            action.status = "denied"
            action.result_json = {"reason": "approval_timeout"}
            await _release_action_reservations(db, action)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        telemetry.emit_alert(
            telemetry.LabAlert.APPROVAL_TIMEOUT,
            run_id=approval.run_id, approval_id=approval.id, reason="expired",
        )
        raise ApprovalInvalid("expired")

    try:
        approval.decision = "approved" if approve else "denied"
        approval.decided_by = decider_user_id
        approval.decided_at = now
        action.status = "approved" if approve else "denied"
        if not approve:
            action.result_json = {"reason": "approval_denied"}
            await _release_action_reservations(db, action)
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return approval


async def decide_approval(db, *, approval_id, decider_user_id, approve, task_owner_id,
                          is_admin=False) -> LabApproval:
    return await _decide_approval(
        db,
        approval_id=approval_id,
        decider_user_id=decider_user_id,
        approve=approve,
        task_owner_id=task_owner_id,
        is_admin=is_admin,
    )


async def _expire_pending_approval(
    db, *, action_id: str, expected_epoch: int
) -> LabToolAction:
    """Atomically converge an elapsed pending approval to a denied action."""
    now = _now()
    action = await db.scalar(
        select(LabToolAction)
        .where(LabToolAction.id == action_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if action is None:
        raise ApprovalInvalid("missing_action")
    if action.fencing_epoch != expected_epoch:
        raise ApprovalInvalid("stale_epoch")
    if action.approval_id is None:
        raise ApprovalInvalid("missing_approval")
    approval = await db.scalar(
        select(LabApproval)
        .where(
            LabApproval.id == action.approval_id,
            LabApproval.action_id == action.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if approval is None or approval.fencing_epoch != expected_epoch:
        raise ApprovalInvalid("approval_binding")

    if approval.decision == "approved":
        if action.status != "approved":
            raise ApprovalInvalid("approved_action_mismatch")
        await db.commit()
        return action
    if approval.decision == "denied":
        if action.status != "denied":
            raise ApprovalInvalid("denied_action_mismatch")
        if not action.result_json:
            action.result_json = {"reason": "approval_denied"}
        await db.commit()
        return action
    if approval.decision not in {"pending", "expired"}:
        raise ApprovalInvalid(f"invalid_decision:{approval.decision}")
    if approval.decision == "pending" and _aware(approval.expires_at) > now:
        raise ApprovalRequired(action_id=action.id, approval_id=approval.id)
    if action.status not in {"waiting_approval", "denied"}:
        raise ApprovalInvalid(f"timeout_action_state:{action.status}")

    newly_expired = approval.decision == "pending"
    approval.decision = "expired"
    approval.decided_at = approval.decided_at or now
    action.status = "denied"
    action.result_json = {"reason": "approval_timeout"}
    if newly_expired:
        await _release_action_reservations(db, action)
    await db.commit()
    telemetry.emit_alert(
        telemetry.LabAlert.APPROVAL_TIMEOUT,
        run_id=approval.run_id,
        approval_id=approval.id,
        reason="expired",
    )
    return action


async def expire_pending_approval(
    db, *, action_id: str, expected_epoch: int
) -> LabToolAction:
    try:
        return await _expire_pending_approval(
            db, action_id=action_id, expected_epoch=expected_epoch
        )
    except BaseException:
        await db.rollback()
        raise


async def execute_action(db, *, action_id, claims, executor, args, expected_epoch=None,
                         on_event=None) -> LabToolAction:
    """Execute a previously-requested action through ``executor(tool_name, args)``.

    Gates, in order: idempotent short-circuit on terminal states; digest binding;
    re-evaluated policy + active grant + egress (a forged approval dies here);
    atomic one-shot approval consumption; an atomic approved->executing claim
    (rowcount check) that serialises execution even on the allow passthrough
    where there is no approval row; then the executor, with the ``executing``
    intent committed first so a crash is reconcilable.
    """
    now = _now()
    action = await db.get(LabToolAction, action_id)
    if action is None:
        raise BrokerError(f"unknown action {action_id}")

    # Idempotent short-circuit — never re-run a terminal/in-flight action.
    if action.status in _NO_REEXEC:
        return action
    if action.status == "executing":
        raise ApprovalInvalid("already_executing")
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
        if action.approval_id is not None:
            approval = await db.scalar(
                select(LabApproval)
                .where(LabApproval.id == action.approval_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if approval is not None and approval.consumed_at is None:
                approval.decision = "denied"
                approval.decided_at = approval.decided_at or now
        try:
            await _release_action_reservations(db, action)
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        await _emit(on_event, action)
        raise ActionDenied(reason, decision=decision, action=action, hard=hard)

    try:
        await grants.check_grant_active(db, claims, expected_epoch=expected_epoch)
    except grants.GrantError as exc:
        await _deny_now(f"grant_inactive: {exc}", hard=True)

    # Lease reconciliation before execution (same structural fence as request):
    # a taken-over owner whose token still verifies is denied because its epoch
    # no longer matches the lease authority. No lease → epoch 0 → passes.
    try:
        await leases.assert_epoch(db, run_id=claims.run_id, epoch=claims.fencing_epoch)
    except leases.StaleEpoch:
        await _deny_now("stale_epoch", hard=True)

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
        approval_id = action.approval_id
        approval = await db.scalar(
            select(LabApproval)
            .where(LabApproval.id == approval_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if approval is None:
            raise ApprovalInvalid("missing_approval")
        if approval.decision == "denied":
            await _deny_now("approval_denied")
        if approval.decision == "pending":
            raise ApprovalRequired(action_id=action.id, approval_id=approval.id)
        if approval.decision == "expired":
            try:
                action.status = "denied"
                action.result_json = {"reason": "approval_timeout"}
                await _release_action_reservations(db, action)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
            raise ApprovalInvalid("expired")
        if _aware(approval.expires_at) <= now:
            try:
                approval.decision = "expired"
                approval.decided_at = approval.decided_at or now
                action.status = "denied"
                action.result_json = {"reason": "approval_timeout"}
                await _release_action_reservations(db, action)
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
            telemetry.emit_alert(
                telemetry.LabAlert.APPROVAL_TIMEOUT,
                run_id=approval.run_id,
                approval_id=approval.id,
                reason="expired",
            )
            raise ApprovalInvalid("expired")
        # decision == "approved": the conditional UPDATE + rowcount check is the
        # real gate — exactly one caller can flip consumed_at from NULL. The
        # ``fencing_epoch`` predicate makes the consume epoch-bound *on the same
        # atomic write* (not a separate check): a caller can only consume an
        # approval minted under its own epoch, so a takeover between the lease
        # reconciliation above and this UPDATE (TOCTOU) cannot let a cross-epoch
        # consume land.
        stmt = (
            update(LabApproval)
            .where(
                LabApproval.id == action.approval_id,
                LabApproval.decision == "approved",
                LabApproval.consumed_at.is_(None),
                LabApproval.expires_at > now,
                LabApproval.fencing_epoch == claims.fencing_epoch,
            )
            .values(consumed_at=now)
            .execution_options(synchronize_session=False)
        )
        result = await db.execute(stmt)
        if result.rowcount != 1:
            await db.rollback()
            approval = await db.get(LabApproval, approval_id)
            if (
                approval is not None
                and approval.consumed_at is None
                and _aware(approval.expires_at) <= _now()
            ):
                action = await db.get(LabToolAction, action_id)
                if action is not None and action.status == "approved":
                    approval.decision = "expired"
                    approval.decided_at = approval.decided_at or _now()
                    action.status = "denied"
                    action.result_json = {"reason": "approval_timeout"}
                    await _release_action_reservations(db, action)
                    await db.commit()
                    raise ApprovalInvalid("expired")
            raise ApprovalInvalid("not_consumable")

    # 7. Atomically claim the action for execution: approved -> executing via a
    # conditional UPDATE + rowcount check (mirrors the approval consume). This is
    # the sole status gate — exactly one caller can win, which closes the
    # allow-passthrough double-execution race where there is no approval row to
    # serialize on. Committed (together with the consume, same txn) before the
    # side effect so a mid-flight crash leaves a durable reconciliation trail.
    claim = (
        update(LabToolAction)
        .where(
            LabToolAction.id == action.id,
            LabToolAction.status == "approved",
            LabToolAction.fencing_epoch == claims.fencing_epoch,
        )
        .values(status="executing", attempts=LabToolAction.attempts + 1)
        .execution_options(synchronize_session=False)
    )
    claimed = await db.execute(claim)
    if claimed.rowcount != 1:
        # Lost the race, or the action was never in an executable state. Roll
        # back (also discards any just-made consume) and re-read the committed
        # state to return the right outcome without ever reaching the executor.
        # Use the ``action_id`` argument, not ``action.id`` — rollback expires
        # the ORM object and touching it would trigger a sync lazy load.
        await db.rollback()
        fresh = await db.get(LabToolAction, action_id)
        if fresh is None:
            raise BrokerError(f"unknown action {action_id}")
        if fresh.status in _NO_REEXEC:
            return fresh
        if fresh.status == "executing":
            raise ApprovalInvalid("already_executing")
        if fresh.status == "denied":
            raise ActionDenied(_reason_of(fresh), action=fresh)
        if fresh.status == "waiting_approval":
            raise ApprovalRequired(action_id=fresh.id, approval_id=fresh.approval_id)
        raise ApprovalInvalid(f"not_executable:{fresh.status}")

    await db.commit()
    await db.refresh(action)  # sync the ORM object to the claimed DB state
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
        is_egress = _is_egress_action(decision.tool, args)
        try:
            action.status = "failed"
            action.result_json = {"error": guard.redact_text(str(exc))}
            await budgets.stage_action_settlement(
                db,
                run_id=claims.run_id,
                succeeded=False,
                is_egress=is_egress,
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
        await _emit(on_event, action)
        return action

    is_egress = _is_egress_action(decision.tool, args)
    egress_bytes = (
        len(protocol.canonical_json(result).encode("utf-8")) if is_egress else 0
    )
    try:
        action.status = "succeeded"
        if isinstance(result, (dict, list)):
            action.result_json = guard.redact_payload(result)
        else:
            action.result_json = {"result": guard.redact_payload(result)}
        exhausted = await budgets.stage_action_settlement(
            db,
            run_id=claims.run_id,
            succeeded=True,
            is_egress=is_egress,
            egress_bytes=egress_bytes,
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    await _emit(on_event, action)
    if exhausted is not None:
        telemetry.emit_alert(
            telemetry.LabAlert.BUDGET_EXHAUSTED,
            run_id=claims.run_id,
            tenant_id=claims.tenant_id,
            dimension=exhausted,
            reason="limit",
        )
        raise budgets.BudgetExhausted(exhausted)
    return action
