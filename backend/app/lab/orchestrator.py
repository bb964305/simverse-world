"""Lab Agent v1 orchestrator (PRD §Control Plane, P1 vertical slice).

This is where "Thin D" becomes real on the Mock runtime. ``run_one_v1`` keeps
the *external* contract of the legacy ``runner.run_one`` byte-for-byte — the
same task/run state machine, the same ``lab_task_update`` / ``lab_run_step`` /
``lab_run_approval`` WS messages, the same ``mark_review`` / ``fail_task``
hooks and refund semantics — but replaces the trust internals: the runtime only
*intends* a tool call, and every effect is threaded through

    grant → lease/fencing → policy/broker → ledger/outbox → budgets →
    (approval) → artifact → mark_review → Compiler.

The runtime (adapter) is not trusted: the Broker re-derives every gate, the
Ledger fences a stale writer, the Budget ledger caps spend, and world effects
go through the Compiler (never the legacy hard-coded ``add_lore``). The feature
flag ``settings.lab_agent_v1_enabled`` selects this path; flag-off preserves the
legacy body exactly (``runner.run_one``), which is the rollback story.

Design resolutions (landed interfaces win over the brief sketch):

* **seq is orchestrator-owned** (T4 handoff): every envelope takes its ``seq``
  from ``ledger.next_seq`` immediately before append; the unique (run_id, seq)
  constraint only guards against duplicates/gaps, it never assigns.
* **Non-tool steps** (think / observation / message — anything with no
  ``ev.tool``) map to a single ``plan.updated`` event. Tool steps run the
  Broker, which yields ``tool.started`` + ``tool.completed`` (whose projection
  is the "observation" the legacy UI showed), so the adapter's own narrative
  observation is not separately re-emitted.
* **Approval is out-of-band from the adapter** (v1): the pause polls the
  ``lab_approvals`` row a REST decision flips (``broker.decide_approval``), not
  the adapter's ``approve`` hook nor ``run.approvals_json``.
* **Execute-time settlement of the tool_calls reservation** (T5 handoff): the
  Broker settles the reservation on a clean success/failure, but an approval
  denial or an execute-time refusal leaves it reserved — the orchestrator
  releases it on those paths.
* **Fencing** (T4 handoff): a ``StaleEpoch`` from a heartbeat or an event append
  means a takeover fenced this owner; the orchestrator writes no terminal state
  and revokes nothing, leaving the run to the new owner.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, UTC

from sqlalchemy import select

from app.config import settings
from app.database import async_session
from app.lab import broker, budgets, compiler, grants, guard, leases, ledger
from app.lab.protocol import RunEventEnvelope
from app.lab.runner import _ws_run_approval, _ws_run_step, _ws_task_update
from app.lab.sandbox import get_adapter
from app.lab.sandbox.base import RunSpec
from app.models.lab_action import LabApproval
from app.models.lab_artifact import LabArtifact
from app.models.lab_run import LabRun, LabRunStep
from app.models.lab_task import LabTask
from app.services import lab_task_service

logger = logging.getLogger(__name__)

# Approval poll cadence. The bound is ``settings.lab_approval_timeout_s`` (then
# default-deny, per legacy ``_await_decision``); this is just how often we
# re-read while waiting, releasing the shared connection on each pass.
_POLL_INTERVAL_S = 0.2
_WORLD_LOCATION_ID = "experiment_building"
_SUMMARY_FALLBACK = "研究完成"


class _RunFailed(Exception):
    """A fatal, refundable run failure (scope/grant/egress denial or budget
    exhaustion). Bubbles to the terminal-failure path — distinct from a
    per-step denial that the run can survive."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


async def _mock_executor(tool_name: str, args: dict) -> dict:
    """Mock effect: echo a redaction-safe summary, no real I/O. The Broker
    redacts and stores whatever this returns."""
    return {"tool": tool_name, "ok": True, "summary": f"executed {tool_name} (mock)"}


async def run_one_v1(run_id: str) -> None:
    """Execute a single queued run through the v1 control plane. Same idempotent
    guard as legacy: only picks up runs still ``queued``."""
    async with async_session() as db:
        run = await db.get(LabRun, run_id)
        if run is None:
            logger.warning("lab run %s vanished before execution", run_id)
            return
        if run.status != "queued":
            return  # already picked up / terminal
        task = await db.get(LabTask, run.task_id)
        if task is None:
            run.status = "failed"
            run.error = "task missing"
            run.ended_at = datetime.now(UTC)
            await db.commit()
            return
        await _Orchestrator(db, run, task).execute()


class _Orchestrator:
    """Per-run v1 control-plane driver. Scalar identifiers are cached as plain
    values so event emission never touches a possibly-expired ORM object; the
    ``run`` / ``task`` rows are only read for WS payloads and re-fetched after
    the approval poll."""

    def __init__(self, db, run: LabRun, task: LabTask):
        self.db = db
        self.run = run
        self.task = task
        self.run_id = run.id
        self.task_id = task.id
        self.tenant_id = task.issuer_user_id
        self.actor = run.researcher_slug or "runtime"
        self.owner_id = f"orchestrator:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.epoch = 0
        self.policy_version = settings.lab_policy_version
        self.token = None
        self.claims = None
        self.adapter = None
        self.handle = None
        self.cost_cents = 0

    # ── lifecycle ─────────────────────────────────────────────────────

    async def execute(self) -> None:
        db = self.db
        fenced = False
        try:
            # Take the run lease FIRST — before touching run/task state. If another
            # owner already holds a live lease (queue redelivery / concurrent
            # double-take), acquire raises LeaseError("held") and we abandon the
            # run untouched below, rather than flipping it to running and then
            # (wrongly) refunding + revoking the holder's grant.
            lease = await leases.acquire_lease(db, run_id=self.run_id, owner_id=self.owner_id)
            self.epoch = lease.fencing_epoch

            # We own the run → the same opening transition as legacy, then WS.
            self.run.status = "running"
            self.run.started_at = datetime.now(UTC)
            self.run.heartbeat_at = datetime.now(UTC)
            self.task.status = "running"
            self.task.updated_at = datetime.now(UTC)
            await db.commit()
            await _ws_task_update(self.task)

            # Open the budget ledger, mint the signed grant, log run.started —
            # before any tool intent.
            await budgets.init_run_budget(db, run_id=self.run_id, tenant_id=self.tenant_id)
            self.token, self.claims = await grants.issue_run_grant(
                db, tenant_id=self.tenant_id, task_id=self.task_id, run_id=self.run_id,
                agent_id=self.actor, capabilities=list(self.run.scopes_json or []),
                egress=list(getattr(settings, "lab_egress_allowlist", []) or []),
                fencing_epoch=self.epoch,
            )
            self.policy_version = self.claims.policy_version
            await self._emit(type="run.started",
                             payload={"adapter": self.run.adapter,
                                      "scopes": list(self.run.scopes_json or [])})

            await self._event_loop()
            await self._succeed()
        except leases.LeaseError as exc:
            # We do not (or no longer) own this run's lease: a concurrent owner
            # holds it ("held") or a takeover fenced us (StaleEpoch, a LeaseError
            # subclass — this single handler covers both). Abandon quietly: write
            # no terminal state, do NOT refund, and let ``finally`` skip revoke so
            # the holder's grant survives. Fencing must never be invertible.
            fenced = True
            logger.warning("lab run %s not owned (%s); abandoning to lease holder",
                           self.run_id, exc)
            return
        except _RunFailed as exc:
            await self._fail(exc.reason)
        except Exception as exc:  # noqa: BLE001 — any adapter/runtime error fails the run
            logger.warning("lab run %s failed: %s", self.run_id, exc, exc_info=True)
            await self._fail(str(exc))
        finally:
            if not fenced:
                # Terminal state reached → every grant for the run is revoked.
                await grants.revoke_run_grants(db, self.run_id)

    async def _event_loop(self) -> None:
        db = self.db
        adapter = get_adapter(self.run.adapter)
        spec = RunSpec(
            run_id=self.run_id, task_id=self.task_id, researcher_slug=self.actor,
            brief=(self.task.brief_md or self.task.title or ""),
            scopes=list(self.run.scopes_json or []),
            budget_usd=(self.run.budget_usd_cents or 0) / 100.0,
            deadline=self.task.deadline_at,
            egress_allowlist=list(getattr(settings, "lab_egress_allowlist", []) or []),
            secrets={}, deliverable_kind=self.task.deliverable_kind,
        )
        self.adapter = adapter
        self.handle = await adapter.start(spec)
        await adapter.submit_goal(self.handle, spec.brief, spec.scopes)

        async for ev in adapter.step_stream(self.handle):
            # Heartbeat the lease each step; a takeover fences us here (StaleEpoch
            # propagates → no terminal write, new owner takes over).
            await leases.heartbeat(db, run_id=self.run_id, owner_id=self.owner_id, epoch=self.epoch)
            self.run.heartbeat_at = datetime.now(UTC)
            self.cost_cents += int(ev.cost_usd_cents or 0)
            if ev.tool:
                await self._handle_tool(ev)
            else:
                await self._emit(type="plan.updated",
                                 payload={"phase": ev.phase,
                                          "summary": guard.redact_text(ev.summary) or ""})

    # ── tool intents through the Broker ───────────────────────────────

    async def _handle_tool(self, ev) -> None:
        db = self.db
        tool = ev.tool
        args = dict(ev.payload or {})
        summary = guard.redact_text(ev.summary) or ""

        try:
            action = await broker.request_action(
                db, claims=self.claims, token=self.token, tool_name=tool,
                args=args, expected_epoch=self.epoch,
            )
        except broker.ActionDenied as exc:
            # A request-time refusal (missing capability / hard deny / egress) is
            # the agent reaching outside its grant — fatal, refund the task
            # (legacy ScopeViolation parity). The Broker already left a denied
            # audit row; no reservation was taken at this stage.
            await self._emit(type="policy.decided", action_id=exc.action.id if exc.action else None,
                             payload={"tool": tool, "decision": "deny", "reason": exc.reason})
            raise _RunFailed(f"denied:{exc.reason}") from exc
        except budgets.BudgetExhausted as exc:
            # Terminal for the run; the Broker already revoked grants + left a
            # denied audit row.
            await self._emit(type="budget.exhausted", payload={"dimension": exc.dimension})
            raise _RunFailed(f"budget_exhausted:{exc.dimension}") from exc

        if action.status == "waiting_approval":
            approved = await self._await_approval(action, summary)
            if not approved:
                # Owner denied (or timed out): the reservation the request took is
                # neither confirmed nor released by the Broker — release it here
                # (T5 handoff). The run survives a denied sensitive action.
                await budgets.release(db, run_id=self.run_id, dimension="tool_calls")
                return

        action_id = action.id  # capture before expiring (avoids a sync lazy-load)
        await self._emit(type="tool.started", action_id=action_id,
                         payload={"tool": tool, "summary": summary})
        # Lock the cross-session freshness invariant instead of leaning on
        # identity-map weak-ref GC: expire our cached action so execute_action
        # re-reads the REST-decided status/approval from the DB, not the stale
        # pre-approval snapshot. (The approval row is deliberately never held
        # strongly across the poll, so execute_action's own db.get re-reads it.)
        db.expire(action)
        try:
            result = await broker.execute_action(
                db, action_id=action_id, claims=self.claims, executor=_mock_executor,
                args=args, expected_epoch=self.epoch,
            )
        except broker.ActionDenied as exc:
            # Execute-time refusal (grant revoked / policy re-eval / approval
            # denied): the Broker did not settle the reservation — release it.
            await budgets.release(db, run_id=self.run_id, dimension="tool_calls")
            await self._emit(type="policy.decided", action_id=action.id,
                             payload={"tool": tool, "decision": "deny", "reason": exc.reason})
            return
        except broker.ApprovalRequired:
            await budgets.release(db, run_id=self.run_id, dimension="tool_calls")
            return
        except broker.ApprovalInvalid as exc:
            # T3 handoff: an in-flight / no-longer-consumable approval
            # (``already_executing`` and friends) must NOT be retried — treat the
            # action as running elsewhere and skip the step, keeping the run
            # alive. No release: a concurrent winner owns the reservation's
            # settlement (avoids a double-release race; the Mock path never hits
            # this — it is purely a takeover-reconciliation guard).
            logger.warning("lab run %s tool %s approval invalid (%s); skipping step",
                           self.run_id, tool, exc.reason)
            return

        result_summary = summary
        if isinstance(result.result_json, dict):
            result_summary = guard.redact_text(str(result.result_json.get("summary", ""))) or summary
        await self._emit(type="tool.completed", action_id=action.id,
                         payload={"tool": tool, "summary": result_summary or f"{tool} 完成"})

    async def _await_approval(self, action, summary: str) -> bool:
        db = self.db
        await self._emit(type="approval.requested", action_id=action.id,
                         payload={"approval_id": action.approval_id, "tool": action.tool_name,
                                  "summary": summary})
        self.run.status = "needs_approval"
        await db.commit()
        await _ws_run_approval(self.task, self.run, action.approval_id, summary)

        approved = await self._poll_decision(action.approval_id)

        # Re-fetch the ORM rows: the poll's commits keep them live (the test
        # factory is expire_on_commit=False), but re-reading is cheap insurance
        # against any caller wired to a default sessionmaker.
        self.run = await db.get(LabRun, self.run_id)
        self.task = await db.get(LabTask, self.task_id)
        self.run.status = "running"
        await db.commit()
        await self._emit(type="approval.resolved", action_id=action.id,
                         payload={"approval_id": action.approval_id,
                                  "decision": "approved" if approved else "denied"})
        return approved

    async def _poll_decision(self, approval_id: str) -> bool:
        db = self.db
        timeout = float(settings.lab_approval_timeout_s or 0)
        waited = 0.0
        while waited < timeout:
            decision = (await db.execute(
                select(LabApproval.decision).where(LabApproval.id == approval_id)
            )).scalar_one_or_none()
            # Release the shared connection between polls so the REST decider can
            # commit; expire_on_commit=False keeps run/task populated.
            await db.commit()
            if decision in ("approved", "denied", "expired"):
                return decision == "approved"
            await asyncio.sleep(_POLL_INTERVAL_S)
            waited += _POLL_INTERVAL_S
        return False  # default-deny on timeout (spec §5.3)

    # ── terminal paths ────────────────────────────────────────────────

    async def _succeed(self) -> None:
        db = self.db
        artifacts = await self.adapter.collect_artifacts(self.handle)
        for _ in artifacts:
            await budgets.reserve(db, run_id=self.run_id, dimension="artifact_count")
        for a in artifacts:
            db.add(LabArtifact(
                run_id=self.run_id, task_id=self.task_id, kind=a.kind, title=a.title,
                uri=a.uri, text_md=a.text_md, meta_json=(a.meta or None),
            ))
        await db.commit()
        for _ in artifacts:
            await budgets.confirm(db, run_id=self.run_id, dimension="artifact_count")
        await self.adapter.stop(self.handle)

        summary = "; ".join(a.title for a in artifacts) if artifacts else _SUMMARY_FALLBACK
        await self._emit(type="artifact.emitted",
                         payload={"count": len(artifacts), "summary": guard.redact_text(summary) or ""})

        self.run.status = "succeeded"
        self.run.ended_at = datetime.now(UTC)
        self.run.cost_usd_cents = self.cost_cents
        await lab_task_service.mark_review(db, self.task, self.run, result_summary=summary)

        # An exploration task (deliverable_kind=world_change) drafts a pending
        # proposal through the Compiler — the only sanctioned path into the world
        # (never the legacy hard-coded add_lore). A compile failure is a warning,
        # not a run failure (legacy try/except parity).
        if self.task.deliverable_kind == "world_change":
            try:
                proposal = await compiler.compile_draft(
                    db,
                    draft={
                        "kind": "add_lore",
                        "patch": {"location_id": _WORLD_LOCATION_ID, "text": summary},
                        "title": f"探索产出：{self.task.title}"[:200],
                        "rationale": summary,
                    },
                    origin_ref=self.run_id, author_slug=self.actor, tenant_id=self.tenant_id,
                )
                await self._emit(type="proposal.drafted",
                                 payload={"proposal_id": proposal.id, "kind": proposal.kind})
            except Exception:  # noqa: BLE001 — a bad draft must not fail a completed run
                logger.warning("proposal draft from run %s failed", self.run_id, exc_info=True)

        await self._emit(type="run.completed", payload={"summary": guard.redact_text(summary) or ""})
        await _ws_task_update(self.task)

    async def _fail(self, reason: str) -> None:
        db = self.db
        self.run = await db.get(LabRun, self.run_id)
        if self.run is not None:
            self.run.status = "failed"
            self.run.ended_at = datetime.now(UTC)
            self.run.error = str(reason)[:500]
            await db.commit()
        try:
            await self._emit(type="run.failed", payload={"reason": str(reason)[:200]})
        except leases.StaleEpoch:
            raise  # fenced mid-failure → let the outer handler abandon
        except Exception:  # noqa: BLE001
            logger.warning("run.failed event append failed for %s", self.run_id, exc_info=True)
        self.task = await db.get(LabTask, self.task_id)
        try:
            await lab_task_service.fail_task(db, self.task, reason=f"run_failed:{self.run_id}")
            await _ws_task_update(self.task)
        except Exception:  # noqa: BLE001
            logger.error("lab task fail/refund failed for %s", self.task_id, exc_info=True)

    # ── ledger append + WS projection ─────────────────────────────────

    async def _emit(self, *, type: str, payload: dict, action_id: str | None = None) -> None:
        db = self.db
        seq = await ledger.next_seq(db, self.run_id)
        envelope = RunEventEnvelope(
            event_id=str(uuid.uuid4()), tenant_id=self.tenant_id, run_id=self.run_id,
            task_id=self.task_id, seq=seq, type=type, actor=self.actor,
            action_id=action_id, fencing_epoch=self.epoch,
            policy_version=self.policy_version, occurred_at=datetime.now(UTC),
            payload=payload or {},
        )
        await ledger.append_event(
            db, envelope=envelope, expected_epoch=self.epoch, outbox_topic="lab_run_event",
        )
        # Forward the legacy-UI compat projection over WS (data source is now the
        # ledger's LabRunStep, not a runner-authored step).
        if ledger.project_step(envelope) is not None:
            step = (await db.execute(
                select(LabRunStep).where(LabRunStep.run_id == self.run_id)
                .order_by(LabRunStep.seq.desc()).limit(1)
            )).scalar_one_or_none()
            if step is not None:
                await _ws_run_step(self.task, self.run, step)
