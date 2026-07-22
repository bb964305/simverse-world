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
* **Action-bound budget settlement**: the Broker owns every reservation from
  request through denial, timeout, execution, and reconciliation. The
  orchestrator never mutates a run-level reservation on an action's behalf.
* **Fencing** (T4 handoff): a ``StaleEpoch`` from a heartbeat or an event append
  means a takeover fenced this owner; the orchestrator writes no terminal state
  and revokes nothing, leaving the run to the new owner.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
import weakref
from datetime import datetime, timedelta, UTC

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import async_session
from app.lab import (
    broker,
    budgets,
    compiler,
    control_plane,
    grants,
    guard,
    leases,
    ledger,
    runtime_sessions,
    supervision,
    workers,
)
from app.lab.protocol import RunEventEnvelope
from app.lab.runner import _ws_run_approval, _ws_run_step, _ws_task_update
from app.lab.sandbox import get_adapter
from app.lab.sandbox.base import (
    ArtifactSpec,
    RunSpec,
    RuntimeV2NonRetryableError,
)
from app.models.lab_action import LabApproval, LabToolAction
from app.models.lab_artifact import LabArtifact, LabArtifactOperation
from app.models.lab_budget import LabRunBudget
from app.models.lab_event import LabRunEvent
from app.models.lab_lease import LabRunLease
from app.models.lab_control import LabToolExecution
from app.models.lab_run import LabRun, LabRunStep
from app.models.lab_runtime import (
    LabRuntimeIntent,
    LabRuntimeResult,
    LabRuntimeSession,
)
from app.models.lab_task import LabTask
from app.models.world_change_proposal import WorldChangeProposal
from app.services import lab_artifact_service, lab_task_service

logger = logging.getLogger(__name__)

# Approval poll cadence. The bound is ``settings.lab_approval_timeout_s`` (then
# default-deny, per legacy ``_await_decision``); this is just how often we
# re-read while waiting, releasing the shared connection on each pass.
_POLL_INTERVAL_S = 0.2
_V2_IDLE_TIMEOUT_S = leases.HEARTBEAT_INTERVAL_S * 3
_WORLD_LOCATION_ID = "experiment_building"
_SUMMARY_FALLBACK = "研究完成"
_V2_ARTIFACT_FINALIZATION_KEY = "_simverse_runtime_v2_finalization"
_EXECUTOR_OUTPUT_META_KEY = "_simverse_executor_output"
_V2_PROCESS_NAMESPACE = uuid.uuid4()
_V2_RUN_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _now_ms() -> int:
    """Monotonic wall-clock source for budget accounting, in milliseconds.
    A module-level indirection so tests can patch the clock and drive the
    ``wall_clock_ms`` dimension deterministically without real sleeps."""
    return int(time.monotonic() * 1000)


def _v2_owner_id(
    run_id: str, *, process_namespace: uuid.UUID | None = None
) -> str:
    """Stable within one process/run, unique across process boots and replicas."""
    namespace = process_namespace or _V2_PROCESS_NAMESPACE
    return str(uuid.uuid5(namespace, run_id))


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


# Code/shell tools (R1) that route through the rootless OCI sandbox when it is
# enabled + configured. Network tools never run here — egress stays governed by
# the Broker/isolation layer, not the container.
#
# fs.write is deliberately EXCLUDED: its args are ``{path, content}``, not a
# command, so ``_command_from_args`` finds nothing to run and the OCI path
# would always report ``ok=False`` (a regression vs. the flag-off Mock, which
# always succeeds). OCI executor v1 only routes tools that carry an executable
# command; fs.write joins once scratch-file materialisation lands.
_OCI_TOOLS = frozenset({"code.run", "shell.exec"})
_EGRESS_TOOLS = frozenset({"web.search", "web.fetch", "browser.navigate"})


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
        # Set true the moment we learn we no longer own the run (a takeover fenced
        # us). It gates the terminal writes AND the ``finally`` revoke: a fenced
        # owner must write no terminal state and must NOT revoke the new owner's
        # grants. Owned by ``execute`` / ``_succeed`` / ``_fail``.
        self.fenced = False
        self.policy_version = settings.lab_policy_version
        self.token = None
        self.claims = None
        self.adapter = None
        self.handle = None
        self.cost_cents = 0
        # Lazily-created, reused for every action of THIS run (never re-created
        # per action): a teardown failure marks the instance's own ``_broken``
        # quarantine, and that must persist across the run's later actions, not
        # reset on the next tool call (see ``_select_executor``).
        self._oci_executor = None
        # active_workers is a gauge: this owner reserves exactly one slot once it
        # starts stepping and releases it on EVERY terminal path (see ``execute``
        # finally). The flag guards the release so a run that never reserved (a
        # held-lease abandon) does not spuriously free a slot it never took.
        self._worker_reserved = False
        # wall_clock_ms is billed as the growing delta of a monotonic clock; the
        # start stamp is taken when the step loop begins.
        self._wall_start_ms: int | None = None
        self._wall_spent_ms = 0

    # ── lifecycle ─────────────────────────────────────────────────────

    async def execute(self) -> None:
        db = self.db
        try:
            # Take the run lease FIRST — before touching run/task state. If another
            # owner already holds a live lease (queue redelivery / concurrent
            # double-take), acquire raises LeaseError("held") and we abandon the
            # run untouched below, rather than flipping it to running and then
            # (wrongly) refunding + revoking the holder's grant.
            lease = await leases.acquire_lease(db, run_id=self.run_id, owner_id=self.owner_id)
            self.epoch = lease.fencing_epoch

            # If we TOOK OVER a previously-owned run (the lease epoch is now past
            # 0), fence the prior owner structurally: revoke every grant issued
            # under an earlier epoch so a stale holder's token fails
            # check_grant_active within its TTL — belt-and-braces with the
            # Broker's lease reconciliation. Our own grant is minted below at
            # ``self.epoch`` (not below it), so this never revokes it.
            if self.epoch > 0:
                await grants.revoke_grants_before_epoch(db, run_id=self.run_id, epoch=self.epoch)

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

            # Take this owner's active_workers slot now that the run is started
            # (after run.started so an exhaustion event orders after it). Mock is
            # single-worker so this always fits, but the reservation + terminal
            # release is the structure P4 concurrency builds on.
            await self._reserve_worker()

            await self._event_loop()
            await self._succeed()
        except leases.LeaseError as exc:
            # We do not (or no longer) own this run's lease: a concurrent owner
            # holds it ("held") or a takeover fenced us (StaleEpoch, a LeaseError
            # subclass — this single handler covers both). Abandon quietly: write
            # no terminal state, do NOT refund, and let ``finally`` skip revoke so
            # the holder's grant survives. Fencing must never be invertible.
            self.fenced = True
            logger.warning("lab run %s not owned (%s); abandoning to lease holder",
                           self.run_id, exc)
            return
        except _RunFailed as exc:
            await self._fail(exc.reason)
        except Exception as exc:  # noqa: BLE001 — any adapter/runtime error fails the run
            logger.warning("lab run %s failed: %s", self.run_id, exc, exc_info=True)
            await self._fail(str(exc))
        finally:
            if self._worker_reserved:
                # Release this owner's active_workers slot on EVERY terminal path,
                # INCLUDING fenced. This owner reserved exactly one slot at start,
                # so releasing exactly one is balanced: a takeover owner holds its
                # OWN separate reservation, and release() floors at zero, so this
                # can neither double-free the new owner's slot nor go negative.
                # (Skipping it on fence would instead leak this owner's slot
                # forever.) Independent of the grant revoke below — this is the
                # owner's own bookkeeping, not a grant of the new owner.
                await budgets.release(db, run_id=self.run_id, dimension="active_workers")
            if not self.fenced:
                # Terminal state reached → every grant for the run is revoked. A
                # fenced owner (lease lost / taken over mid-terminal) skips this
                # so it never revokes the NEW owner's grants (fencing must never
                # invert — the P1 _fail StaleEpoch bug).
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

        self._wall_start_ms = _now_ms()
        async for ev in adapter.step_stream(self.handle):
            # Heartbeat the lease each step; a takeover fences us here (StaleEpoch
            # propagates → no terminal write, new owner takes over).
            await leases.heartbeat(db, run_id=self.run_id, owner_id=self.owner_id, epoch=self.epoch)
            self.run.heartbeat_at = datetime.now(UTC)
            self.cost_cents += int(ev.cost_usd_cents or 0)
            # Hard budgets billed per step, before the step's effect: wall-clock
            # as the elapsed-since-last-step delta, then this step's model tokens.
            # Either exhausting drives the standard budget termination.
            await self._charge_wall_clock()
            await self._spend("model_tokens", int(ev.model_tokens or 0))
            if ev.tool:
                await self._handle_tool(ev)
            elif ev.phase == "delegate":
                await self._handle_delegation(ev)
            else:
                await self._emit(type="plan.updated",
                                 payload={"phase": ev.phase,
                                          "summary": guard.redact_text(ev.summary) or ""})

    # ── specialist-worker delegation (P4) ─────────────────────────────

    async def _handle_delegation(self, ev) -> None:
        """The runtime intends to delegate to a specialist worker. Issue an
        attenuated, role-scoped child grant — ``workers.delegate_worker``
        enforces the depth-1 capability subset (role∩parent, never an
        escalation) and the concurrency cap of 3. A cap/role refusal is
        non-fatal: the run continues with the workers it already has. Child
        grants are revoked with the run's grants on any terminal path
        (``execute`` finally → ``revoke_run_grants``), so cancellation cleans
        them up. The event payload is content-free (role/agent id/caps/jti)."""
        payload = ev.payload or {}
        role = str(payload.get("role") or "")
        agent_id = str(payload.get("agent_id") or f"{role or 'worker'}-{uuid.uuid4().hex[:6]}")
        sub_goal = str(payload.get("sub_goal") or payload.get("summary") or "")
        try:
            _token, child = await workers.delegate_worker(
                self.db, parent_claims=self.claims, role=role, agent_id=agent_id,
                sub_goal=sub_goal,
            )
        except workers.WorkerLimitError:
            await self._emit(type="agent.delegated",
                             payload={"role": role, "agent_id": agent_id, "refused": "worker_cap"})
            return
        except workers.WorkerRoleError:
            await self._emit(type="agent.delegated",
                             payload={"role": role, "agent_id": agent_id, "refused": "unknown_role"})
            return
        await self._emit(type="agent.delegated",
                         payload={"role": role, "agent_id": agent_id,
                                  "child_jti": child.jti, "depth": child.depth,
                                  "capabilities": list(child.capabilities)})
        # Supervised bounded execution of the child on Mock, then join its result
        # into the parent event stream and finish it (revoke grant + release slot).
        # A Verifier's failed verdict is surfaced so the parent never accepts a
        # Builder artifact behind an unvalidated verification (P4 §validate).
        result = await workers.execute_worker_on_mock(
            self.db, child_claims=child, role=role, sub_goal=sub_goal)
        await self._emit(type="agent.worker_completed",
                         payload={"role": role, "agent_id": agent_id, "child_jti": child.jti,
                                  "status": result.status, "result_digest": result.result_digest,
                                  "verdict": result.verdict})
        await workers.finish_worker(
            self.db, jti=child.jti, status=result.status, result_digest=result.result_digest)

    # ── hard-budget spend helpers ─────────────────────────────────────

    async def _terminate_budget(self, dimension: str) -> None:
        """Emit the single budget-exhaustion event and raise the refundable
        failure. Grants are revoked and the task refunded by the terminal path
        (``execute``'s ``finally`` + ``_fail``); the Broker revokes eagerly for
        the dimensions it owns, this covers the orchestrator-owned ones."""
        await self._emit(type="budget.exhausted", payload={"dimension": dimension})
        raise _RunFailed(f"budget_exhausted:{dimension}")

    async def _spend(self, dimension: str, amount: int) -> None:
        """Direct-debit ``amount`` of ``dimension``; an exhaustion becomes the
        standard budget termination. Zero/negative is a no-op (nothing to bill)."""
        if amount <= 0:
            return
        try:
            await budgets.spend(self.db, run_id=self.run_id, dimension=dimension, amount=amount)
        except budgets.BudgetExhausted as exc:
            await self._terminate_budget(exc.dimension)

    async def _charge_wall_clock(self) -> None:
        """Bill the wall-clock elapsed since the last checkpoint (monotonic
        delta, never negative). The checkpoint advances before the spend so a
        subsequent step bills only the new increment."""
        if getattr(self, "_wall_start_ms", None) is None:
            return
        elapsed = _now_ms() - self._wall_start_ms
        delta = elapsed - self._wall_spent_ms
        if delta > 0:
            self._wall_spent_ms = elapsed
            await self._spend("wall_clock_ms", delta)

    async def _reserve_worker(self) -> None:
        """Reserve this owner's single active_workers slot; an exhaustion (P4
        multi-worker over-subscription) drives the standard termination. On
        success, mark the slot so the terminal ``finally`` releases exactly it."""
        try:
            await budgets.reserve(self.db, run_id=self.run_id, dimension="active_workers")
        except budgets.BudgetExhausted as exc:
            await self._terminate_budget(exc.dimension)
        self._worker_reserved = True

    # ── executor selection ───────────────────────────────────────────

    def _executor_output_id(
        self, *, action_id: str, epoch: int, relative_path: str
    ) -> str:
        return str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            "simverse:executor-output:"
            f"{self.run_id}:{action_id}:{epoch}:{relative_path}",
        ))

    def _executor_provider_artifact_id(
        self, *, action_id: str, epoch: int, relative_path: str
    ) -> str:
        return str(uuid.uuid5(
            uuid.NAMESPACE_URL,
            "simverse:executor-provider-artifact:"
            f"{self.run_id}:{action_id}:{epoch}:{relative_path}",
        ))

    @staticmethod
    def _executor_contract_deadline(contract: dict) -> datetime:
        raw = contract.get("deadline_at")
        if not isinstance(raw, str):
            raise ValueError("Executor output contract has no deadline")
        try:
            deadline = datetime.fromisoformat(
                raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            )
        except ValueError as exc:
            raise ValueError("Executor output contract deadline is invalid") from exc
        if deadline.tzinfo is None or deadline.utcoffset() is None:
            raise ValueError("Executor output contract deadline must be aware")
        return deadline.astimezone(UTC)

    def _validate_executor_contract(
        self,
        contract: dict,
        *,
        action_id: str,
        epoch: int,
        tool_name: str,
        args: dict,
    ):
        from app.lab import protocol

        expected_keys = {
            "schema_version",
            "run_id",
            "session_id",
            "action_id",
            "epoch",
            "tool_name",
            "args_digest",
            "base_url",
            "image_digest",
            "limits",
            "deadline_at",
        }
        if not isinstance(contract, dict) or set(contract) != expected_keys:
            raise ValueError("Executor output contract shape is invalid")
        if (
            contract["schema_version"] != 1
            or contract["run_id"] != self.run_id
            or contract["action_id"] != action_id
            or contract["epoch"] != epoch
            or contract["tool_name"] != tool_name
            or contract["args_digest"] != protocol.args_digest(args)
            or not isinstance(contract["session_id"], str)
            or not contract["session_id"]
            or not isinstance(contract["base_url"], str)
            or not contract["base_url"]
            or not isinstance(contract["image_digest"], str)
        ):
            raise ValueError("Executor output contract binding changed")
        limits = protocol.ExecutorResourceLimits.model_validate(
            contract["limits"], strict=True
        )
        deadline_at = self._executor_contract_deadline(contract)
        return limits, deadline_at

    async def _prepare_executor_output_specs(
        self,
        *,
        action_id: str,
        epoch: int,
        tool_name: str,
        args: dict,
        executor_base_url: str,
    ):
        """Persist output declarations, then create/replay only attempt-1 leases."""
        from app.lab import protocol
        from app.lab.artifact_pipeline import (
            ArtifactPipelineClient,
            ArtifactPipelineError,
            ArtifactReceiptError,
        )
        from app.lab.artifact_services.canonical import canonical_digest
        from app.lab.artifact_services.schemas import UploadLeaseCommand

        declarations = protocol.executor_output_declarations(args)
        if not declarations:
            return [], None
        if not settings.lab_artifact_pipeline_enabled:
            raise ArtifactPipelineError(
                "Executor outputs require the production artifact pipeline"
            )

        await self._lock_current_authority()
        action = await self.db.scalar(
            select(LabToolAction)
            .where(LabToolAction.id == action_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            action is None
            or action.run_id != self.run_id
            or action.task_id != self.task_id
            or action.tenant_id != self.tenant_id
            or action.tool_name != tool_name
            or action.args_hash != protocol.args_digest(args)
            or action.fencing_epoch != epoch
            or action.status not in {"executing", "reconciliation_required"}
        ):
            raise supervision.RuntimeProtocolConflict(
                "Executor output action binding changed"
            )

        rows = (
            await self.db.execute(
                select(LabArtifact).where(
                    LabArtifact.run_id == self.run_id,
                    LabArtifact.producer_action_id == action_id,
                )
            )
        ).scalars().all()
        output_rows = [
            row for row in rows
            if isinstance((row.meta_json or {}).get(_EXECUTOR_OUTPUT_META_KEY), dict)
        ]
        persisted_contract: dict | None = None
        if output_rows:
            persisted_contract = dict(
                (output_rows[0].meta_json or {})[_EXECUTOR_OUTPUT_META_KEY].get(
                    "command", {}
                )
            )
            self._validate_executor_contract(
                persisted_contract,
                action_id=action_id,
                epoch=epoch,
                tool_name=tool_name,
                args=args,
            )

        if persisted_contract is None:
            if not self.runtime_session_id:
                raise supervision.RuntimeProtocolConflict(
                    "Executor output has no durable Runtime session"
                )
            deadline_at = datetime.now(UTC) + timedelta(
                seconds=settings.lab_executor_job_timeout_s
            )
            limits = protocol.ExecutorResourceLimits(
                wall_clock_ms=settings.lab_executor_job_timeout_s * 1000,
                cpu_millis=settings.lab_executor_job_cpu_millis,
                memory_bytes=settings.lab_executor_job_memory_bytes,
                pids=settings.lab_executor_job_pids,
                stdout_bytes=settings.lab_executor_job_stdout_bytes,
                stderr_bytes=settings.lab_executor_job_stderr_bytes,
                scratch_bytes=settings.lab_executor_job_scratch_bytes,
            )
            persisted_contract = {
                "schema_version": 1,
                "run_id": self.run_id,
                "session_id": self.runtime_session_id,
                "action_id": action_id,
                "epoch": epoch,
                "tool_name": tool_name,
                "args_digest": protocol.args_digest(args),
                "base_url": executor_base_url,
                "image_digest": settings.lab_executor_image_digest,
                "limits": limits.model_dump(mode="json"),
                "deadline_at": deadline_at.isoformat().replace("+00:00", "Z"),
            }
        limits, deadline_at = self._validate_executor_contract(
            persisted_contract,
            action_id=action_id,
            epoch=epoch,
            tool_name=tool_name,
            args=args,
        )
        if sum(item.max_bytes for item in declarations) > limits.scratch_bytes:
            raise supervision.RuntimeProtocolConflict(
                "Executor output declarations exceed scratch capacity"
            )
        if action.deadline_at is not None:
            action_deadline = action.deadline_at
            if action_deadline.tzinfo is None:
                action_deadline = action_deadline.replace(tzinfo=UTC)
            if action_deadline.astimezone(UTC) != deadline_at:
                raise supervision.RuntimeProtocolConflict(
                    "Executor action deadline changed across replay"
                )
        else:
            action.deadline_at = deadline_at

        expected_ids = {
            self._executor_output_id(
                action_id=action_id,
                epoch=epoch,
                relative_path=item.relative_path,
            )
            for item in declarations
        }
        if any(row.id not in expected_ids for row in output_rows):
            raise supervision.RuntimeProtocolConflict(
                "Executor output declaration set changed across replay"
            )
        by_id = {row.id: row for row in output_rows}
        missing: list[LabArtifact] = []
        persisted: list[LabArtifact] = []
        for index, declaration in enumerate(declarations):
            artifact_id = self._executor_output_id(
                action_id=action_id,
                epoch=epoch,
                relative_path=declaration.relative_path,
            )
            provider_artifact_id = self._executor_provider_artifact_id(
                action_id=action_id,
                epoch=epoch,
                relative_path=declaration.relative_path,
            )
            declaration_json = declaration.model_dump(mode="json")
            marker = {
                "schema_version": 1,
                "output_index": index,
                "output_count": len(declarations),
                "declaration": declaration_json,
                "declaration_digest": protocol.content_digest(declaration_json),
                "command": persisted_contract,
            }
            artifact = by_id.get(artifact_id)
            if artifact is None:
                occupied = await self.db.get(LabArtifact, artifact_id)
                if occupied is not None:
                    raise supervision.RuntimeProtocolConflict(
                        "Executor output artifact id is already occupied"
                    )
                artifact = LabArtifact(
                    id=artifact_id,
                    run_id=self.run_id,
                    task_id=self.task_id,
                    kind=declaration.kind,
                    title=declaration.title,
                    uri=None,
                    text_md=None,
                    meta_json={_EXECUTOR_OUTPUT_META_KEY: marker},
                    provider_artifact_id=provider_artifact_id,
                    runtime_session_id=persisted_contract["session_id"],
                    provider_session_id=persisted_contract["session_id"],
                    producer_epoch=epoch,
                    required=declaration.required,
                    declared_content_type=declaration.content_type,
                    content_type=declaration.content_type,
                    original_filename=declaration.original_filename,
                    expected_sha256=declaration.expected_sha256,
                    declared_byte_size=None,
                    storage_status="pending_upload",
                    provenance="runtime",
                )
                await lab_artifact_service.finalize_artifact(
                    self.db,
                    artifact=artifact,
                    tenant_id=self.tenant_id,
                    producer_action_id=action_id,
                    scanned_clean=False,
                )
                missing.append(artifact)
            elif any((
                artifact.run_id != self.run_id,
                artifact.task_id != self.task_id,
                artifact.kind != declaration.kind,
                artifact.title != declaration.title,
                artifact.uri is not None,
                artifact.text_md is not None,
                artifact.meta_json != {_EXECUTOR_OUTPUT_META_KEY: marker},
                artifact.tenant_id != self.tenant_id,
                artifact.provider_artifact_id != provider_artifact_id,
                artifact.runtime_session_id != persisted_contract["session_id"],
                artifact.provider_session_id != persisted_contract["session_id"],
                artifact.producer_epoch != epoch,
                artifact.required is not declaration.required,
                artifact.declared_content_type != declaration.content_type,
                artifact.original_filename != declaration.original_filename,
                artifact.expected_sha256 != declaration.expected_sha256,
                artifact.declared_byte_size is not None,
                artifact.producer_action_id != action_id,
                artifact.provenance != "runtime",
            )):
                raise supervision.RuntimeProtocolConflict(
                    "Executor output artifact changed across replay"
                )
            persisted.append(artifact)

        if missing:
            budget = await self.db.scalar(
                select(LabRunBudget)
                .where(LabRunBudget.run_id == self.run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if budget is not None:
                if (
                    budget.limit_artifact_count
                    and budget.used_artifact_count
                    + budget.reserved_artifact_count
                    + len(missing)
                    > budget.limit_artifact_count
                ):
                    budget.exhausted_dimension = "artifact_count"
                    await self.db.commit()
                    raise budgets.BudgetExhausted("artifact_count")
                budget.used_artifact_count += len(missing)
            self.db.add_all(missing)
        try:
            await self.db.commit()
        except IntegrityError as exc:
            await self.db.rollback()
            raise supervision.RuntimeProtocolConflict(
                "Executor output artifact persistence raced"
            ) from exc

        pipeline = ArtifactPipelineClient.from_settings()
        specs: list[protocol.ExecutorOutputSpec] = []
        try:
            for artifact, declaration in zip(persisted, declarations, strict=True):
                latest = await self.db.scalar(
                    select(LabArtifactOperation)
                    .where(
                        LabArtifactOperation.artifact_id == artifact.id,
                        LabArtifactOperation.operation_type == "upload",
                    )
                    .order_by(
                        LabArtifactOperation.attempt.desc(),
                        LabArtifactOperation.created_at.desc(),
                    )
                    .limit(1)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if latest is not None:
                    try:
                        lease_command = UploadLeaseCommand.model_validate(
                            latest.command_json, strict=True
                        )
                    except ValueError as exc:
                        raise ArtifactReceiptError(
                            "durable Executor upload command is invalid"
                        ) from exc
                    if (
                        latest.attempt != 1
                        or latest.operation_id != lease_command.upload_id
                        or latest.command_digest != canonical_digest(lease_command)
                        or lease_command.max_bytes != declaration.max_bytes
                        or lease_command.expected_sha256
                        != declaration.expected_sha256
                        or lease_command.expires_at < deadline_at
                        or latest.state not in {"pending", "processing"}
                        or (
                            latest.state == "pending"
                            and lease_command.expires_at <= datetime.now(UTC)
                        )
                    ):
                        raise ArtifactPipelineError(
                            "Executor output lease cannot be rebound or retried"
                        )
                lease, operation = await pipeline.create_upload_lease(
                    self.db,
                    artifact=artifact,
                    max_bytes=declaration.max_bytes,
                )
                if (
                    operation.attempt != 1
                    or operation.artifact_id != artifact.id
                    or operation.epoch != epoch
                    or lease.expires_at < deadline_at
                ):
                    raise ArtifactReceiptError(
                        "Executor output lease changed across replay"
                    )
                specs.append(protocol.ExecutorOutputSpec(
                    **declaration.model_dump(mode="python"),
                    artifact_id=artifact.id,
                    lease=lease,
                ))
        finally:
            await pipeline.aclose()
        return specs, persisted_contract

    @staticmethod
    def _executor_result_payload(tool_name: str, envelope) -> dict:
        result = envelope.result
        summary = result.stdout.strip() or result.stderr.strip()
        return {
            "tool": tool_name,
            "ok": result.state == "succeeded",
            "state": result.state,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "summary": summary[:1000] or f"executor {result.state}",
            "teardown": result.teardown_proof,
            "artifact_receipts": result.artifact_receipts,
            "result_digest": result.result_digest,
            "executor_receipt": envelope.receipt.model_dump(mode="json"),
        }

    async def _persist_executor_envelope(self, *, action_id: str, epoch: int, envelope) -> None:
        await control_plane.record_executor_result_receipt(
            self.db,
            action_id=action_id,
            epoch=epoch,
            result_receipt=envelope.receipt.model_dump(mode="json"),
        )
        if envelope.result.state != "reconciliation_required":
            await control_plane.settle_executor_target(
                self.db,
                action_id=action_id,
                epoch=epoch,
                teardown_proof=envelope.result.teardown_proof,
            )
        await self.db.commit()

    async def _apply_executor_outputs(self, *, command, envelope) -> str | None:
        from app.lab import protocol
        from app.lab.artifact_pipeline import (
            ArtifactPipelineClient,
            ArtifactPipelineError,
            ArtifactReceiptError,
        )
        from app.lab.remote_executor import (
            RemoteExecutorClient,
            RemoteExecutorProtocolError,
        )

        returned = RemoteExecutorClient.validate_declared_outputs(
            envelope.result, command
        )
        if not command.outputs:
            return None
        pipeline = ArtifactPipelineClient.from_settings()
        errors: list[str] = []
        artifact_budget_exhausted = False
        try:
            for spec in command.outputs:
                artifact_envelope = returned.get(spec.artifact_id)
                if artifact_envelope is None:
                    if spec.required:
                        errors.append(
                            f"required_executor_output_missing:{spec.artifact_id}"
                        )
                    continue
                await self._lock_current_authority()
                artifact = await self.db.scalar(
                    select(LabArtifact)
                    .where(LabArtifact.id == spec.artifact_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                declaration = protocol.RuntimeExecutorOutputDeclaration(
                    relative_path=spec.relative_path,
                    kind=spec.kind,
                    expected_use=spec.expected_use,
                    title=spec.title,
                    content_type=spec.content_type,
                    original_filename=spec.original_filename,
                    required=spec.required,
                    max_bytes=spec.max_bytes,
                    expected_sha256=spec.expected_sha256,
                )
                marker = (
                    (artifact.meta_json or {}).get(_EXECUTOR_OUTPUT_META_KEY)
                    if artifact is not None
                    else None
                )
                contract = (
                    marker.get("command") if isinstance(marker, dict) else None
                )
                try:
                    limits, deadline_at = self._validate_executor_contract(
                        contract,
                        action_id=command.action_id,
                        epoch=command.epoch,
                        tool_name=command.tool_name,
                        args=command.args,
                    )
                except (TypeError, ValueError) as exc:
                    await self.db.rollback()
                    raise RemoteExecutorProtocolError(
                        "Executor output metadata contract is invalid"
                    ) from exc
                if (
                    artifact is None
                    or artifact.run_id != command.run_id
                    or artifact.task_id != self.task_id
                    or artifact.tenant_id != self.tenant_id
                    or artifact.provider_session_id != command.session_id
                    or artifact.producer_action_id != command.action_id
                    or artifact.producer_epoch != command.epoch
                    or artifact.kind != spec.kind
                    or artifact.title != spec.title
                    or artifact.required is not spec.required
                    or artifact.declared_content_type != spec.content_type
                    or artifact.original_filename != spec.original_filename
                    or artifact.expected_sha256 != spec.expected_sha256
                    or not isinstance(marker, dict)
                    or marker.get("declaration")
                    != declaration.model_dump(mode="json")
                    or marker.get("declaration_digest")
                    != protocol.content_digest(marker.get("declaration"))
                    or limits != command.limits
                    or deadline_at != command.deadline_at.astimezone(UTC)
                    or contract.get("session_id") != command.session_id
                    or contract.get("image_digest") != command.image_digest
                ):
                    await self.db.rollback()
                    raise RemoteExecutorProtocolError(
                        "Executor output artifact binding changed"
                    )

                apply_error: str | None = None
                try:
                    artifact = await pipeline.apply_upload_receipt(
                        self.db,
                        receipt_value=artifact_envelope.upload_receipt.model_dump(
                            mode="json"
                        ),
                        commit=False,
                    )
                except ArtifactReceiptError as exc:
                    await self.db.rollback()
                    raise RemoteExecutorProtocolError(
                        "Executor output receipt failed verification"
                    ) from exc
                except ArtifactPipelineError as exc:
                    apply_error = str(exc)

                operation = await self.db.scalar(
                    select(LabArtifactOperation)
                    .where(
                        LabArtifactOperation.operation_id
                        == spec.lease.upload_id
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if (
                    operation is None
                    or operation.artifact_id != artifact.id
                    or operation.operation_type != "upload"
                    or operation.epoch != command.epoch
                    or operation.receipt_digest
                    != artifact_envelope.upload_receipt_digest
                    or artifact.byte_size
                    != artifact_envelope.manifest.byte_size
                    or artifact.sha256 != artifact_envelope.manifest.sha256
                ):
                    await self.db.rollback()
                    raise RemoteExecutorProtocolError(
                        "Executor output durable receipt binding changed"
                    )
                if operation.accounted_at is None:
                    budget = await self.db.scalar(
                        select(LabRunBudget)
                        .where(LabRunBudget.run_id == self.run_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if budget is not None:
                        if (
                            budget.limit_artifact_bytes
                            and budget.used_artifact_bytes
                            + budget.reserved_artifact_bytes
                            + artifact.byte_size
                            > budget.limit_artifact_bytes
                        ):
                            budget.exhausted_dimension = "artifact_bytes"
                            artifact_budget_exhausted = True
                        budget.used_artifact_bytes += artifact.byte_size
                    operation.accounted_at = datetime.now(UTC)
                if operation.state != "succeeded":
                    apply_error = (
                        operation.error_code
                        or apply_error
                        or "artifact_upload_failed"
                    )
                await self.db.commit()

                if operation.state == "succeeded" and artifact.scan_status == "pending":
                    await pipeline.submit_scan(self.db, artifact=artifact)
                if spec.required and apply_error:
                    errors.append(
                        f"required_executor_output_rejected:{apply_error}"
                    )
                if spec.required and artifact.scan_status in {"flagged", "failed"}:
                    errors.append(
                        "required_executor_output_scan:"
                        f"{artifact.scan_error_code or artifact.scan_status}"
                    )
        finally:
            await pipeline.aclose()
        if artifact_budget_exhausted:
            raise budgets.BudgetExhausted("artifact_bytes")
        return errors[0] if errors else None

    async def _wait_executor_result(
        self, *, client, command, binding, deadline_at
    ):
        from app.lab.executor_service.schemas import EXECUTOR_TERMINAL_STATES
        from app.lab.remote_executor import (
            RemoteExecutorError,
            RemoteExecutorProtocolError,
        )

        while True:
            try:
                status = await client.get_status(binding)
            except RemoteExecutorProtocolError:
                raise
            except RemoteExecutorError:
                if datetime.now(UTC) >= deadline_at:
                    raise broker.UncertainOutcome(
                        "remote Executor status remained uncertain"
                    ) from None
                await asyncio.sleep(settings.lab_executor_poll_interval_s)
                continue
            if status.state in EXECUTOR_TERMINAL_STATES:
                break
            if datetime.now(UTC) >= deadline_at:
                raise broker.UncertainOutcome(
                    "remote Executor job exceeded its durable deadline"
                )
            await asyncio.sleep(settings.lab_executor_poll_interval_s)
        while True:
            try:
                envelope = await client.get_result(binding)
                client.validate_declared_outputs(envelope.result, command)
                return envelope
            except RemoteExecutorProtocolError:
                raise
            except RemoteExecutorError:
                if datetime.now(UTC) >= deadline_at:
                    raise broker.UncertainOutcome(
                        "remote Executor result remained uncertain"
                    ) from None
                await asyncio.sleep(settings.lab_executor_poll_interval_s)

    @staticmethod
    def _executor_client_for_url(base_url: str):
        from app.lab.remote_executor import (
            RemoteExecutorClient,
            configured_executor_auth,
        )

        issuer, verifier = configured_executor_auth()
        return RemoteExecutorClient(
            base_url=base_url,
            token_issuer=issuer,
            receipt_verifier=verifier,
            timeout=settings.lab_executor_request_timeout_s,
            poll_interval=settings.lab_executor_poll_interval_s,
        )

    async def _prepare_executor_command(
        self,
        *,
        client,
        action_id: str,
        epoch: int,
        tool_name: str,
        args: dict,
    ):
        from app.lab import protocol

        outputs, contract = await self._prepare_executor_output_specs(
            action_id=action_id,
            epoch=epoch,
            tool_name=tool_name,
            args=args,
            executor_base_url=client.base_url,
        )
        if contract is None:
            deadline_at = datetime.now(UTC) + timedelta(
                seconds=settings.lab_executor_job_timeout_s
            )
            limits = protocol.ExecutorResourceLimits(
                wall_clock_ms=settings.lab_executor_job_timeout_s * 1000,
                cpu_millis=settings.lab_executor_job_cpu_millis,
                memory_bytes=settings.lab_executor_job_memory_bytes,
                pids=settings.lab_executor_job_pids,
                stdout_bytes=settings.lab_executor_job_stdout_bytes,
                stderr_bytes=settings.lab_executor_job_stderr_bytes,
                scratch_bytes=settings.lab_executor_job_scratch_bytes,
            )
            session_id = self.runtime_session_id
            image_digest = settings.lab_executor_image_digest
        else:
            limits, deadline_at = self._validate_executor_contract(
                contract,
                action_id=action_id,
                epoch=epoch,
                tool_name=tool_name,
                args=args,
            )
            session_id = contract["session_id"]
            image_digest = contract["image_digest"]
            if client.base_url != contract["base_url"]:
                client = self._executor_client_for_url(contract["base_url"])
        if not session_id:
            raise supervision.RuntimeProtocolConflict(
                "remote Executor requires a durable Runtime session"
            )
        command = client.build_command(
            run_id=self.run_id,
            session_id=session_id,
            action_id=action_id,
            epoch=epoch,
            tool_name=tool_name,
            args=args,
            image_digest=image_digest,
            limits=limits,
            deadline_at=deadline_at,
            outputs=outputs,
        )
        return client, command

    def _remote_executor(
        self,
        *,
        action_id: str,
        intended_tool_name: str,
        intended_args: dict,
    ):
        from app.lab import protocol
        from app.lab.remote_executor import (
            ExecutorJobBinding,
            RemoteExecutorProtocolError,
            configured_remote_executor,
            executor_job_locator,
        )

        if not self.runtime_session_id:
            raise RuntimeError("remote Executor requires a durable Runtime session")
        client = configured_remote_executor()
        prepared: dict = {}

        async def prepare() -> None:
            prepared_client, command = await self._prepare_executor_command(
                client=client,
                action_id=action_id,
                epoch=self.epoch,
                tool_name=intended_tool_name,
                args=intended_args,
            )
            locator = executor_job_locator(
                base_url=prepared_client.base_url,
                command=command,
            )
            await self._lock_current_authority()
            await control_plane.register_executor_target(
                self.db,
                run_id=self.run_id,
                action_id=action_id,
                job_locator=locator,
                epoch=command.epoch,
                session_id=command.session_id,
            )
            prepared["client"] = prepared_client
            prepared["command"] = command
            prepared["deadline_at"] = command.deadline_at

        async def execute(tool_name: str, args: dict) -> dict:
            command = prepared.get("command")
            deadline_at = prepared.get("deadline_at")
            prepared_client = prepared.get("client")
            if command is None or deadline_at is None or prepared_client is None:
                raise broker.UncertainOutcome(
                    "Executor command was not durably prepared"
                )
            if (
                tool_name != command.tool_name
                or protocol.args_digest(args) != protocol.args_digest(command.args)
            ):
                raise broker.UncertainOutcome(
                    "Executor invocation diverged from its durable command"
                )
            try:
                status = await prepared_client.submit(command)
                await control_plane.record_executor_submit_receipt(
                    self.db,
                    action_id=action_id,
                    epoch=command.epoch,
                    submit_receipt=status.submit_receipt.model_dump(mode="json"),
                )
                await self.db.commit()
                binding = ExecutorJobBinding.from_command(command)
                envelope = await self._wait_executor_result(
                    client=prepared_client,
                    command=command,
                    binding=binding,
                    deadline_at=deadline_at,
                )
                await self._persist_executor_envelope(
                    action_id=action_id,
                    epoch=command.epoch,
                    envelope=envelope,
                )
            except broker.UncertainOutcome:
                raise
            except Exception as exc:
                raise broker.UncertainOutcome(
                    "remote Executor outcome requires reconciliation"
                ) from exc
            payload = self._executor_result_payload(tool_name, envelope)
            try:
                artifact_error = await self._apply_executor_outputs(
                    command=command, envelope=envelope
                )
            except budgets.BudgetExhausted:
                raise
            except RemoteExecutorProtocolError as exc:
                payload["artifact_error"] = guard.redact_text(str(exc))
                raise broker.TerminalExecutionFailure(payload) from exc
            except Exception as exc:
                raise broker.UncertainOutcome(
                    "Executor artifact settlement requires reconciliation"
                ) from exc
            if artifact_error is not None:
                payload["artifact_error"] = artifact_error
            if envelope.result.state == "reconciliation_required":
                raise broker.UncertainOutcome("Executor requires reconciliation")
            if envelope.result.state != "succeeded" or artifact_error is not None:
                raise broker.TerminalExecutionFailure(payload)
            return payload

        return execute, prepare

    def _remote_egress_executor(
        self,
        *,
        action_id: str,
        intended_tool_name: str,
        intended_args: dict,
    ):
        """Build one action-bound client for the independent egress service."""

        from app.lab.egress_service.client import (
            RemoteEgressClient,
            RemoteEgressError,
        )
        from app.lab.egress_service.models import EgressActionCommand
        from app.lab import protocol

        client = RemoteEgressClient.configured()
        command = EgressActionCommand(
            action_id=action_id,
            run_id=self.run_id,
            tool_name=intended_tool_name,
            args=dict(intended_args),
            args_digest=protocol.args_digest(intended_args),
            egress_allowlist=sorted(set(self.claims.egress)),
        )
        command_snapshot = {
            "schema_version": 1,
            "base_url": client.base_url,
            "command": command.model_dump(mode="json"),
        }

        async def prepare() -> None:
            await self._lock_current_authority()
            action = await self.db.scalar(
                select(LabToolAction)
                .where(LabToolAction.id == action_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                action is None
                or action.run_id != self.run_id
                or action.task_id != self.task_id
                or action.tenant_id != self.tenant_id
                or action.fencing_epoch != self.epoch
                or action.status != "executing"
                or action.tool_name != intended_tool_name
                or action.args_hash != command.args_digest
            ):
                raise supervision.RuntimeProtocolConflict(
                    "egress action binding changed before submission"
                )
            existing = (
                action.result_json.get(broker.EGRESS_COMMAND_KEY)
                if isinstance(action.result_json, dict)
                else None
            )
            if existing is not None and existing != command_snapshot:
                raise supervision.RuntimeProtocolConflict(
                    "egress command changed across replay"
                )
            action.result_json = {
                broker.EGRESS_COMMAND_KEY: command_snapshot,
            }

        async def execute(tool_name: str, args: dict):
            if (
                tool_name != command.tool_name
                or protocol.args_digest(args) != command.args_digest
            ):
                raise broker.UncertainOutcome(
                    "egress invocation diverged from its durable command"
                )
            try:
                status = await client.execute(command)
            except RemoteEgressError as exc:
                raise broker.UncertainOutcome(
                    "remote egress outcome requires reconciliation"
                ) from exc
            if status.state == "failed":
                raise broker.TerminalExecutionFailure(
                    {
                        "tool": tool_name,
                        "ok": False,
                        "state": "failed",
                        "error": status.error_code or "egress_failed",
                    },
                    egress_requests=status.usage.requests,
                    egress_bytes=status.usage.bytes,
                )
            if status.state != "succeeded" or status.result is None:
                raise broker.UncertainOutcome("egress action is not terminal")
            return broker.TrustedEgressResult(
                payload=status.result,
                requests=status.usage.requests,
                bytes=status.usage.bytes,
            )

        return execute, prepare

    async def _reconcile_remote_egress_action(self, action, args: dict):
        from app.lab import protocol
        from app.lab.egress_service.client import (
            RemoteEgressClient,
            RemoteEgressError,
            RemoteEgressProtocolError,
        )
        from app.lab.egress_service.models import EgressActionCommand

        try:
            snapshot = (
                action.result_json.get(broker.EGRESS_COMMAND_KEY)
                if isinstance(action.result_json, dict)
                else None
            )
            if (
                not isinstance(snapshot, dict)
                or set(snapshot) != {"schema_version", "base_url", "command"}
                or snapshot.get("schema_version") != 1
                or not isinstance(snapshot.get("base_url"), str)
            ):
                raise ValueError("durable egress command is missing")
            command = EgressActionCommand.model_validate(
                snapshot["command"], strict=True
            )
            if (
                command.action_id != action.id
                or command.run_id != self.run_id
                or command.tool_name != action.tool_name
                or command.args_digest != action.args_hash
                or command.args_digest != protocol.args_digest(args)
            ):
                raise ValueError("durable egress command binding changed")
            configured = RemoteEgressClient.configured()
            client = RemoteEgressClient(
                base_url=snapshot["base_url"],
                api_key=configured.api_key,
                request_timeout_s=configured.request_timeout_s,
                action_timeout_s=configured.action_timeout_s,
                poll_interval_s=configured.poll_interval_s,
            )
            status = await client.get(action.id)
            if status is not None and status.request_digest != command.request_digest:
                raise RemoteEgressProtocolError(
                    "egress request digest changed during reconciliation"
                )
            if status is None or status.state not in {"succeeded", "failed"}:
                status = await client.execute(command)
            if status.state == "failed":
                raise broker.TerminalExecutionFailure(
                    {
                        "tool": action.tool_name,
                        "ok": False,
                        "state": "failed",
                        "error": status.error_code or "egress_failed",
                    },
                    egress_requests=status.usage.requests,
                    egress_bytes=status.usage.bytes,
                )
            if status.state != "succeeded" or status.result is None:
                raise broker.UncertainOutcome("egress action is not terminal")
            result = broker.TrustedEgressResult(
                payload=status.result,
                requests=status.usage.requests,
                bytes=status.usage.bytes,
            )
        except broker.TerminalExecutionFailure as exc:
            return await broker.settle_reconciled_action(
                self.db,
                action_id=action.id,
                result=broker.TrustedEgressResult(
                    payload=exc.result,
                    requests=exc.egress_requests,
                    bytes=exc.egress_bytes,
                ),
                succeeded=False,
            )
        except (broker.UncertainOutcome, RemoteEgressError, ValueError) as exc:
            return await broker.park_reconciled_action(
                self.db,
                action_id=action.id,
                result={
                    "error": guard.redact_text(str(exc)),
                    "uncertain": True,
                },
            )
        return await broker.settle_reconciled_action(
            self.db,
            action_id=action.id,
            result=result,
            succeeded=True,
        )

    async def _reconcile_remote_executor_action(self, action, args: dict | None = None):
        """Resume the exact persisted Executor job after an uncertain/crashed call."""
        from app.lab import protocol
        from app.lab.remote_executor import (
            ExecutorJobBinding,
            RemoteExecutorClient,
            RemoteExecutorError,
            RemoteExecutorProtocolError,
            configured_executor_auth,
            configured_remote_executor,
            executor_job_locator,
        )

        if action.tool_name in _EGRESS_TOOLS:
            if args is None or protocol.args_digest(args) != action.args_hash:
                return await broker.park_reconciled_action(
                    self.db,
                    action_id=action.id,
                    result={
                        "error": "durable egress arguments are missing",
                        "uncertain": True,
                    },
                )
            return await self._reconcile_remote_egress_action(action, args)

        # Unsupported protocol-v2 tools never have an Executor locator.  The
        # normal Broker path rejects them immediately after its durable
        # ``approved -> executing`` claim; a crash in that narrow window must
        # converge to the same deterministic failure instead of being parked as
        # an unknown remote effect.
        if action.tool_name not in _OCI_TOOLS:
            return await broker.settle_reconciled_action(
                self.db,
                action_id=action.id,
                result={"error": f"unsupported_tool:{action.tool_name}"},
                succeeded=False,
            )

        if args is None or protocol.args_digest(args) != action.args_hash:
            return await broker.park_reconciled_action(
                self.db,
                action_id=action.id,
                result={
                    "error": "durable Executor arguments are missing",
                    "uncertain": True,
                },
            )
        try:
            declarations = protocol.executor_output_declarations(args)
        except ValueError as exc:
            return await broker.park_reconciled_action(
                self.db,
                action_id=action.id,
                result={
                    "error": guard.redact_text(str(exc)),
                    "uncertain": True,
                },
            )

        target = await self.db.scalar(
            select(LabToolExecution).where(
                LabToolExecution.run_id == self.run_id,
                LabToolExecution.action_id == action.id,
                LabToolExecution.executor_epoch == action.fencing_epoch,
            )
        )
        if target is None:
            if not declarations:
                return await broker.park_reconciled_action(
                    self.db,
                    action_id=action.id,
                    result={
                        "error": "durable Executor locator is missing",
                        "uncertain": True,
                    },
                )
            try:
                recovery_client, recovery_command = (
                    await self._prepare_executor_command(
                        client=configured_remote_executor(),
                        action_id=action.id,
                        epoch=action.fencing_epoch,
                        tool_name=action.tool_name,
                        args=args,
                    )
                )
                recovered_locator = executor_job_locator(
                    base_url=recovery_client.base_url,
                    command=recovery_command,
                )
                await self._lock_current_authority()
                target = await control_plane.register_executor_target(
                    self.db,
                    run_id=self.run_id,
                    action_id=action.id,
                    job_locator=recovered_locator,
                    epoch=recovery_command.epoch,
                    session_id=recovery_command.session_id,
                )
                await self.db.commit()
            except budgets.BudgetExhausted:
                raise
            except Exception as exc:
                return await broker.park_reconciled_action(
                    self.db,
                    action_id=action.id,
                    result={
                        "error": guard.redact_text(str(exc)),
                        "uncertain": True,
                    },
                )
        locator = dict(target.job_locator_json or {})
        try:
            binding = ExecutorJobBinding.from_locator(locator)
            command = protocol.ExecutorJobCommand.model_validate(
                locator.get("command")
            )
            if ExecutorJobBinding.from_command(command) != binding:
                raise ValueError("Executor command diverges from locator")
            if (
                command.run_id != self.run_id
                or command.action_id != action.id
                or command.tool_name != action.tool_name
                or protocol.args_digest(command.args) != action.args_hash
                or protocol.args_digest(command.args) != protocol.args_digest(args)
            ):
                raise ValueError("Executor command diverges from its Broker action")
            deadline_at = command.deadline_at
            issuer, verifier = configured_executor_auth()
            client = RemoteExecutorClient(
                base_url=str(locator.get("base_url") or ""),
                token_issuer=issuer,
                receipt_verifier=verifier,
                timeout=settings.lab_executor_request_timeout_s,
                poll_interval=settings.lab_executor_poll_interval_s,
            )
        except Exception as exc:
            return await broker.park_reconciled_action(
                self.db,
                action_id=action.id,
                result={
                    "error": guard.redact_text(str(exc)),
                    "uncertain": True,
                    "executor_job_id": str(locator.get("job_id") or ""),
                },
            )
        try:
            try:
                status = await client.get_status(binding)
            except RemoteExecutorProtocolError as exc:
                if (
                    str(exc) != "executor_http_404"
                    or target.submit_receipt_json is not None
                ):
                    raise
                # No accepted receipt plus a durable canonical command is the
                # only state in which the same deterministic job may be
                # submitted again after a pre-POST crash.
                status = await client.submit(command)
            await control_plane.record_executor_submit_receipt(
                self.db,
                action_id=action.id,
                epoch=command.epoch,
                submit_receipt=status.submit_receipt.model_dump(mode="json"),
            )
            await self.db.commit()
            envelope = await self._wait_executor_result(
                client=client,
                command=command,
                binding=binding,
                deadline_at=deadline_at,
            )
            await self._persist_executor_envelope(
                action_id=action.id,
                epoch=command.epoch,
                envelope=envelope,
            )
        except (RemoteExecutorError, broker.UncertainOutcome) as exc:
            return await broker.park_reconciled_action(
                self.db,
                action_id=action.id,
                result={
                    "error": guard.redact_text(str(exc)),
                    "uncertain": True,
                    "executor_job_id": binding.job_id,
                },
            )
        except Exception as exc:
            return await broker.park_reconciled_action(
                self.db,
                action_id=action.id,
                result={
                    "error": guard.redact_text(str(exc)),
                    "uncertain": True,
                    "executor_job_id": binding.job_id,
                },
            )
        payload = self._executor_result_payload(action.tool_name, envelope)
        try:
            artifact_error = await self._apply_executor_outputs(
                command=command, envelope=envelope
            )
        except budgets.BudgetExhausted:
            raise
        except RemoteExecutorProtocolError as exc:
            payload["artifact_error"] = guard.redact_text(str(exc))
            return await broker.settle_reconciled_action(
                self.db,
                action_id=action.id,
                result=payload,
                succeeded=False,
            )
        except Exception as exc:
            return await broker.park_reconciled_action(
                self.db,
                action_id=action.id,
                result={
                    "error": guard.redact_text(str(exc)),
                    "uncertain": True,
                    "executor_job_id": binding.job_id,
                },
            )
        if artifact_error is not None:
            payload["artifact_error"] = artifact_error
        if envelope.result.state == "reconciliation_required":
            return await broker.park_reconciled_action(
                self.db,
                action_id=action.id,
                result=payload,
            )
        return await broker.settle_reconciled_action(
            self.db,
            action_id=action.id,
            result=payload,
            succeeded=(
                envelope.result.state == "succeeded"
                and artifact_error is None
            ),
        )

    def _select_executor(
        self,
        tool_name: str,
        *,
        action_id: str | None = None,
        args: dict | None = None,
    ):
        """Select the Broker executor without weakening the protocol boundary.

        Protocol v2 always uses the independent remote Executor for supported
        tools. Unsupported tools fail inside the Broker state machine and can
        never reach Mock. Protocol v1 retains its explicit local OCI/Mock
        rollback behavior.
        """
        if settings.lab_agent_v2_enabled:
            if action_id is None:
                raise RuntimeError("protocol-v2 Executor requires an action id")
            if args is None:
                raise RuntimeError("protocol-v2 Executor requires bound arguments")
            if tool_name in _EGRESS_TOOLS:
                return self._remote_egress_executor(
                    action_id=action_id,
                    intended_tool_name=tool_name,
                    intended_args=dict(args),
                )
            if tool_name not in _OCI_TOOLS:
                async def reject_unsupported_tool(
                    _tool_name: str, _args: dict
                ) -> dict:
                    raise RuntimeError(f"unsupported_tool:{_tool_name}")

                return reject_unsupported_tool, None
            return self._remote_executor(
                action_id=action_id,
                intended_tool_name=tool_name,
                intended_args=dict(args),
            )
        if settings.lab_oci_enabled and settings.lab_oci_image and tool_name in _OCI_TOOLS:
            if self._oci_executor is None:
                from app.lab.sandbox.oci_executor import OciExecutor, SandboxLimits
                self._oci_executor = OciExecutor(
                    image=settings.lab_oci_image, limits=SandboxLimits(),
                )
            return self._oci_executor.as_broker_executor(), None
        return _mock_executor, None

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
                # The Broker has already settled this action's reservation. The
                # run survives a denied sensitive action.
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
            executor, prepare_executor = self._select_executor(
                tool, action_id=action_id, args=args
            )
            result = await broker.execute_action(
                db, action_id=action_id, claims=self.claims,
                executor=executor,
                args=args, expected_epoch=self.epoch,
                prepare_executor=prepare_executor,
            )
        except broker.ActionDenied as exc:
            # Execute-time refusal is already settled by the Broker.
            await self._emit(type="policy.decided", action_id=action.id,
                             payload={"tool": tool, "decision": "deny", "reason": exc.reason})
            return
        except broker.ApprovalRequired:
            return
        except budgets.BudgetExhausted as exc:
            # egress_bytes over-limit inside execute_action: the action already
            # executed and settled its request-side reservations; the response
            # size pushed egress_bytes past the cap. The effect is done, so we
            # terminate the run (no further steps run) rather than undo it.
            await self._terminate_budget(exc.dimension)
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

    async def _collect_success_artifacts(self):
        return await self.adapter.collect_artifacts(self.handle)

    async def _stop_after_success(self) -> None:
        await self.adapter.stop(self.handle)

    async def _ensure_world_change_proposal(
        self, *, summary: str
    ) -> WorldChangeProposal:
        """Return the run's one exact draft or fail the world deliverable.

        Proposal creation commits independently so a retry must converge on the
        same row. A missing, duplicate, or divergent draft is not compatible with
        a successful ``world_change`` run.
        """
        proposals = (
            await self.db.execute(
                select(WorldChangeProposal)
                .where(
                    WorldChangeProposal.origin == "lab_run",
                    WorldChangeProposal.origin_ref == self.run_id,
                )
                .order_by(
                    WorldChangeProposal.created_at,
                    WorldChangeProposal.id,
                )
            )
        ).scalars().all()
        expected_text = guard.redact_text(summary) or ""
        expected_patch = {
            "location_id": _WORLD_LOCATION_ID,
            "text": expected_text,
        }
        if proposals:
            if len(proposals) != 1:
                raise _RunFailed("world_change_proposal_conflict")
            proposal = proposals[0]
            if (
                proposal.kind != "add_lore"
                or proposal.author_slug != self.actor
                or (proposal.patch_json or {}) != expected_patch
            ):
                raise _RunFailed("world_change_proposal_conflict")
            return proposal

        try:
            return await compiler.compile_draft(
                self.db,
                draft={
                    "kind": "add_lore",
                    "patch": expected_patch,
                    "title": f"探索产出：{self.task.title}"[:200],
                    "rationale": expected_text,
                },
                origin_ref=self.run_id,
                author_slug=self.actor,
            )
        except Exception as exc:  # noqa: BLE001 - deliverable creation is mandatory
            await self.db.rollback()
            raise _RunFailed("world_change_proposal_failed") from exc

    async def _succeed(self) -> None:
        db = self.db
        # Epoch gate: never write a terminal state / settle the task if a takeover
        # has fenced us. Reconcile against the lease authority BEFORE touching
        # run.status, budgets, or mark_review; a stale owner sets ``self.fenced``
        # and returns so the run is left entirely to the new owner (and the
        # ``finally`` revoke is skipped — no grant of the new owner is touched).
        try:
            await leases.assert_epoch(db, run_id=self.run_id, epoch=self.epoch)
        except leases.StaleEpoch:
            self.fenced = True
            return

        artifacts = await self._collect_success_artifacts()
        built = []
        for a in artifacts:
            artifact = LabArtifact(
                run_id=self.run_id, task_id=self.task_id, kind=a.kind, title=a.title,
                uri=a.uri, text_md=a.text_md, meta_json=(a.meta or None),
            )
            # V12: stamp tenant/digest/size/expiry before the row lands (P2-B).
            # finalize only mutates the Python object — nothing is in the session
            # yet, so the budget charges below can fail the run without leaving a
            # half-written artifact. No per-artifact action mapping exists on the
            # Mock runtime, so producer_action_id is None (brief allows it).
            # Trust boundary (gap #10): Mock output is synthetic + safe, so its
            # artifacts release after task completion. A REAL adapter's artifacts
            # stay quarantined (scan_status skipped / unverified) until a real
            # scanner clears them — no unverified real body/URI leaves the API.
            await lab_artifact_service.finalize_artifact(
                db, artifact=artifact, tenant_id=self.tenant_id, producer_action_id=None,
                scanned_clean=(self.run.adapter == "mock"),
            )
            # Hard budgets, charged BEFORE staging the row: reserve one
            # artifact_count unit, then debit artifact_bytes by the finalized
            # size. An exhaustion of either drives the standard termination with
            # nothing persisted — the row is only db.add-ed once all charges clear.
            await budgets.reserve(db, run_id=self.run_id, dimension="artifact_count")
            await self._spend("artifact_bytes", artifact.byte_size or 0)
            built.append(artifact)
        for artifact in built:
            db.add(artifact)
        await db.commit()
        for _ in built:
            await budgets.confirm(db, run_id=self.run_id, dimension="artifact_count")
        await self._stop_after_success()

        summary = "; ".join(a.title for a in artifacts) if artifacts else _SUMMARY_FALLBACK
        await self._emit(type="artifact.emitted",
                         payload={"count": len(artifacts), "summary": guard.redact_text(summary) or ""})

        proposal = None
        if self.task.deliverable_kind == "world_change":
            proposal = await self._ensure_world_change_proposal(summary=summary)
            await self._emit(
                type="proposal.drafted",
                payload={"proposal_id": proposal.id, "kind": proposal.kind},
            )

        # Advance the task first (CAS-guarded). If it was cancelled/finalized
        # concurrently, mark_review is a no-op and returns False: do NOT emit a
        # completion, draft a world proposal, or overwrite the cancel path's run
        # terminal — just return so the ``finally`` still revokes this run's
        # grants. The cancel path owns the refund. (The epoch fence normally trips
        # earlier at ``_emit``; this closes the residual window between the last
        # emit and mark_review.)
        reviewed = await lab_task_service.mark_review(
            db, self.task, self.run, result_summary=summary)
        if not reviewed:
            logger.info("run %s finished but task %s no longer reviewable "
                        "(cancelled/terminal); skipping completion",
                        self.run_id, self.task_id)
            return

        self.run.status = "succeeded"
        self.run.ended_at = datetime.now(UTC)
        self.run.cost_usd_cents = self.cost_cents
        await db.commit()

        await self._emit(type="run.completed", payload={"summary": guard.redact_text(summary) or ""})
        await _ws_task_update(self.task)

    async def _fail(self, reason: str) -> None:
        db = self.db
        # Epoch gate: a takeover fences the terminal write. Reconcile against the
        # lease authority BEFORE flipping run.status or refunding — a stale owner
        # sets ``self.fenced`` and returns, leaving the run (and its escrow) to
        # the new owner. Without this, a fenced loser would fail+refund a run the
        # new owner is still driving, and the ``finally`` would revoke the new
        # owner's grants.
        try:
            await leases.assert_epoch(db, run_id=self.run_id, epoch=self.epoch)
        except leases.StaleEpoch:
            self.fenced = True
            return

        self.run = await db.get(LabRun, self.run_id)
        if self.run is not None:
            self.run.status = "failed"
            self.run.ended_at = datetime.now(UTC)
            self.run.error = str(reason)[:500]
            await db.commit()
        try:
            await self._emit(type="run.failed", payload={"reason": str(reason)[:200]})
        except leases.StaleEpoch:
            # Fenced between the gate above and the event append: mark fenced and
            # return so the ``finally`` skips the revoke. Re-raising here left
            # ``self.fenced`` False and inverted the fence (the P1 bug) — the
            # finally then revoked the NEW owner's grants.
            self.fenced = True
            return
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


async def run_one_v2(run_id: str) -> None:
    """Resume one protocol-v2 run through the durable Runtime result loop."""
    lock = _V2_RUN_LOCKS.get(run_id)
    if lock is None:
        lock = asyncio.Lock()
        _V2_RUN_LOCKS[run_id] = lock
    async with lock:
        async with async_session() as db:
            run = await db.get(LabRun, run_id)
            if run is None:
                logger.warning("lab v2 run %s vanished before execution", run_id)
                return
            if run.protocol_version != 2 or run.adapter != "simverse_ref":
                raise supervision.RuntimeProtocolConflict(
                    "protocol-v2 handler received an incompatible run"
                )
            if run.status not in {"queued", "running", "needs_approval"}:
                return
            task = await db.get(LabTask, run.task_id)
            if task is None:
                run.status = "failed"
                run.error = "task missing"
                run.ended_at = datetime.now(UTC)
                await db.commit()
                return
            await _V2Orchestrator(db, run, task).execute()


class _V2Orchestrator(_Orchestrator):
    """Lease-owned Gateway driver for Runtime protocol-v2."""

    def __init__(self, db, run: LabRun, task: LabTask):
        super().__init__(db, run, task)
        self.owner_id = _v2_owner_id(run.id)
        self.runtime_session_id: str | None = None
        self.provider_session_id: str | None = None
        self.runtime_epoch: int | None = None
        self._completion_bridge = False
        self._recovery_session_id: str | None = None
        self._heartbeat_task: asyncio.Task | None = None

    def _run_spec(self) -> RunSpec:
        return RunSpec(
            run_id=self.run_id,
            task_id=self.task_id,
            researcher_slug=self.actor,
            brief=(self.task.brief_md or self.task.title or ""),
            scopes=list(self.run.scopes_json or []),
            budget_usd=(self.run.budget_usd_cents or 0) / 100.0,
            deadline=self.task.deadline_at,
            egress_allowlist=list(
                getattr(settings, "lab_egress_allowlist", []) or []
            ),
            secrets={},
            deliverable_kind=self.task.deliverable_kind,
        )

    async def _lock_current_authority(self) -> LabRunLease:
        lease = await self.db.scalar(
            select(LabRunLease)
            .where(LabRunLease.run_id == self.run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        expires_at = None if lease is None else lease.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if (
            lease is None
            or lease.owner_id != self.owner_id
            or lease.fencing_epoch != self.epoch
            or expires_at is None
            or expires_at <= datetime.now(UTC)
        ):
            await self.db.rollback()
            self.fenced = True
            raise leases.StaleEpoch(
                f"protocol-v2 owner lost authority for run {self.run_id}"
            )
        return lease

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(leases.HEARTBEAT_INTERVAL_S)
            try:
                async with async_session() as heartbeat_db:
                    await leases.heartbeat(
                        heartbeat_db,
                        run_id=self.run_id,
                        owner_id=self.owner_id,
                        epoch=self.epoch,
                    )
            except asyncio.CancelledError:
                raise
            except leases.StaleEpoch:
                self.fenced = True
                return
            except Exception:  # noqa: BLE001 - the next DB authority gate decides
                logger.warning(
                    "lab v2 lease heartbeat failed for %s",
                    self.run_id,
                    exc_info=True,
                )

    async def execute(self) -> None:
        db = self.db
        try:
            lease = await leases.acquire_lease(
                db, run_id=self.run_id, owner_id=self.owner_id
            )
            self.epoch = lease.fencing_epoch
            if self.epoch > 0:
                await grants.revoke_grants_before_epoch(
                    db, run_id=self.run_id, epoch=self.epoch
                )
            cross_epoch_completed = (
                await self._quarantine_cross_epoch_runtime_session()
            )
            if cross_epoch_completed is not None:
                self._recovery_session_id = cross_epoch_completed.id
            await budgets.init_run_budget(
                db, run_id=self.run_id, tenant_id=self.tenant_id
            )
            self._wall_start_ms = _now_ms()
            self._wall_spent_ms = 0
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            adapter = get_adapter(self.run.adapter)
            spec = self._run_spec()
            runtime_epoch = (
                cross_epoch_completed.fencing_epoch
                if cross_epoch_completed is not None
                else self.epoch
            )
            stable_client_id = (
                cross_epoch_completed.client_run_id
                if cross_epoch_completed is not None
                else runtime_sessions.client_run_id(self.run_id, runtime_epoch)
            )
            prepare = getattr(adapter, "prepare_protocol_v2", None)
            if not callable(prepare):
                raise supervision.HandshakeRejected(
                    "real adapter has no protocol-v2 preparation hook"
                )
            prepare(
                spec=spec,
                epoch=runtime_epoch,
                client_run_id=stable_client_id,
            )
            proof = await supervision.validate_runtime_provider(adapter)
            if cross_epoch_completed is None:
                runtime_session = await runtime_sessions.create_or_reattach(
                    db,
                    run_id=self.run_id,
                    epoch=self.epoch,
                    owner_id=self.owner_id,
                    provider=adapter,
                    durability_class="session_affine",
                )
            else:
                runtime_session = (
                    await runtime_sessions.recover_existing_for_new_authority(
                        db,
                        run_id=self.run_id,
                        authority_epoch=self.epoch,
                        owner_id=self.owner_id,
                        provider=adapter,
                        durability_class="session_affine",
                    )
                )
            if (
                runtime_session.provider_name != proof.manifest.provider_name
                or not runtime_session.provider_session_id
            ):
                raise supervision.RuntimeProtocolConflict(
                    "Runtime registration diverged from its supervision proof"
                )
            self.adapter = adapter
            self.handle = runtime_session.provider_session_id
            self.runtime_session_id = runtime_session.id
            self._recovery_session_id = runtime_session.id
            self.provider_session_id = runtime_session.provider_session_id
            self.runtime_epoch = runtime_session.fencing_epoch
            self._completion_bridge = self.runtime_epoch != self.epoch
            self._runtime_was_completed = runtime_session.status == "completed"

            self.run = await db.scalar(
                select(LabRun)
                .where(LabRun.id == self.run_id)
                .execution_options(populate_existing=True)
            )
            self.task = await db.scalar(
                select(LabTask)
                .where(LabTask.id == self.task_id)
                .execution_options(populate_existing=True)
            )
            if self._runtime_was_completed:
                recoverable_task = self.task.status in {"assigned", "running"} or (
                    self.task.status in {"review", "completed", "rejected"}
                    and self.task.accepted_run_id == self.run_id
                )
                if not recoverable_task:
                    await db.commit()
                    return
            else:
                if self.task.status not in {"assigned", "running"}:
                    await db.commit()
                    return
                self.run.status = "running"
                self.run.started_at = self.run.started_at or datetime.now(UTC)
                self.run.heartbeat_at = datetime.now(UTC)
                if self.task.status in {"assigned", "running"}:
                    self.task.status = "running"
                    self.task.updated_at = datetime.now(UTC)
            await db.commit()
            if not self._runtime_was_completed:
                await _ws_task_update(self.task)

            if self._runtime_was_completed:
                self.policy_version = settings.lab_policy_version
                await self._recover_provider_ack()
                await self._charge_wall_clock()
                await self._finalize_success_v2_bounded()
                return

            await grants.revoke_run_grants(db, self.run_id)
            self.token, self.claims = await grants.issue_run_grant(
                db,
                tenant_id=self.tenant_id,
                task_id=self.task_id,
                run_id=self.run_id,
                agent_id=self.actor,
                capabilities=list(self.run.scopes_json or []),
                egress=list(getattr(settings, "lab_egress_allowlist", []) or []),
                fencing_epoch=self.epoch,
            )
            self.policy_version = self.claims.policy_version
            started = await db.scalar(
                select(LabRunEvent.event_id).where(
                    LabRunEvent.run_id == self.run_id,
                    LabRunEvent.type == "run.started",
                )
            )
            if started is None:
                await self._emit(
                    type="run.started",
                    payload={
                        "adapter": self.run.adapter,
                        "scopes": list(self.run.scopes_json or []),
                        "runtime_session_id": self.runtime_session_id,
                    },
                )
            await self._reserve_worker()
            await self._drive_runtime_v2_bounded()
            await self._finalize_success_v2_bounded()
        except leases.LeaseError:
            self.fenced = True
            raise
        except runtime_sessions.RuntimeSessionInProgress:
            await self._charge_wall_clock()
            raise
        except (
            _RunFailed,
            supervision.HandshakeRejected,
            supervision.RuntimeProtocolConflict,
            broker.RuntimeResultConflict,
            RuntimeV2NonRetryableError,
            runtime_sessions.RuntimeSessionError,
        ) as exc:
            reason = exc.reason if isinstance(exc, _RunFailed) else str(exc)
            if (
                isinstance(exc, RuntimeV2NonRetryableError)
                and self._recovery_session_id is not None
            ):
                await runtime_sessions.quarantine_recovered_session(
                    db,
                    session_id=self._recovery_session_id,
                    run_id=self.run_id,
                    authority_epoch=self.epoch,
                    owner_id=self.owner_id,
                    reason=reason,
                )
            try:
                await self._charge_wall_clock()
            except _RunFailed as budget_failure:
                reason = budget_failure.reason
            await self._fail(reason)
        except Exception:
            try:
                await self._charge_wall_clock()
            except _RunFailed as budget_failure:
                await self._fail(budget_failure.reason)
                return
            logger.warning(
                "lab v2 run %s hit a recoverable delivery failure",
                self.run_id,
                exc_info=True,
            )
            raise
        finally:
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._heartbeat_task
            if self._worker_reserved:
                await budgets.release(
                    db, run_id=self.run_id, dimension="active_workers"
                )
            if not self.fenced:
                await grants.revoke_run_grants(db, self.run_id)

    async def _quarantine_cross_epoch_runtime_session(
        self,
    ) -> LabRuntimeSession | None:
        existing = await self.db.scalar(
            select(LabRuntimeSession)
            .where(LabRuntimeSession.run_id == self.run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if existing is None:
            await self.db.commit()
            return None
        if existing.fencing_epoch == self.epoch:
            if existing.authority_epoch != self.epoch:
                await self.db.rollback()
                raise supervision.RuntimeProtocolConflict(
                    "Runtime authority epoch diverged from its initial binding"
                )
            await self.db.commit()
            return None
        recoverable = existing.status == "ready"
        if existing.status == "completed":
            recoverable = await supervision.runtime_final_ready(
                self.db,
                session_id=existing.id,
                require_real_result=True,
                require_succeeded=True,
            )
        if (
            recoverable
            and existing.fencing_epoch < self.epoch
            and existing.authority_epoch <= self.epoch
        ):
            await self.db.commit()
            return existing
        existing.status = "quarantined"
        existing.last_error = (
            "runtime session belongs to a fenced epoch; takeover requires "
            "explicit reconciliation"
        )
        existing.ended_at = datetime.now(UTC)
        await self.db.commit()
        raise supervision.RuntimeProtocolConflict(existing.last_error)

    async def _runtime_timeout_seconds(self) -> float:
        snapshot = await budgets.snapshot(self.db, self.run_id)
        wall = snapshot.get("wall_clock_ms")
        if wall:
            durable_limit_ms = int(wall["limit"] or settings.lab_budget_wall_clock_ms)
            remaining_ms = (
                durable_limit_ms
                - int(wall["used"])
                - int(wall["reserved"])
            )
        else:
            remaining_ms = int(settings.lab_budget_wall_clock_ms)
        await self.db.commit()
        wall_clock_seconds = remaining_ms / 1000.0
        deadline = self.task.deadline_at
        if deadline is not None:
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            deadline_seconds = (deadline.astimezone(UTC) - datetime.now(UTC)).total_seconds()
            wall_clock_seconds = min(wall_clock_seconds, deadline_seconds)
        if wall_clock_seconds <= 0:
            raise _RunFailed("runtime_timeout")
        return wall_clock_seconds

    async def _drive_runtime_v2_bounded(self) -> None:
        if getattr(self, "_wall_start_ms", None) is None:
            self._wall_start_ms = _now_ms()
            self._wall_spent_ms = 0

        async def drive() -> None:
            if not getattr(self, "_runtime_was_completed", False):
                await self.adapter.submit_goal_v2(
                    provider_session_id=self.provider_session_id
                )
            await self._event_loop_v2()

        try:
            timeout = await self._runtime_timeout_seconds()
            await asyncio.wait_for(
                drive(), timeout=timeout
            )
        except TimeoutError as exc:
            raise _RunFailed("runtime_timeout") from exc
        finally:
            # A transport/status failure can requeue this run before the event
            # loop reaches its next accounting checkpoint. Persist the final
            # interval on every attempt so repeated retryable failures cannot
            # reset and bypass the durable wall-clock budget.
            await self._charge_wall_clock()

    async def _finalize_success_v2_bounded(self) -> None:
        if getattr(self, "_wall_start_ms", None) is None:
            self._wall_start_ms = _now_ms()
            self._wall_spent_ms = 0
        try:
            timeout = await self._runtime_timeout_seconds()
            await asyncio.wait_for(self._succeed(), timeout=timeout)
        except TimeoutError as exc:
            raise _RunFailed("runtime_timeout") from exc
        finally:
            # Finalization includes durable artifact, task, event and proposal
            # writes. Charge that tail on success and on every retryable failure.
            await self._charge_wall_clock()

    async def _recover_provider_ack(self) -> None:
        assert self.runtime_session_id and self.provider_session_id
        session = await self.db.get(LabRuntimeSession, self.runtime_session_id)
        committed = session.provider_cursor_committed
        acked = session.provider_cursor_acked
        blocked = await self.db.scalar(
            select(LabRuntimeIntent.provider_cursor)
            .where(
                LabRuntimeIntent.session_id == self.runtime_session_id,
                LabRuntimeIntent.status == "pending",
            )
            .order_by(LabRuntimeIntent.provider_cursor)
            .limit(1)
        )
        recoverable_through = (
            committed if blocked is None else min(committed, blocked - 1)
        )
        await self.db.commit()
        if recoverable_through <= acked:
            return
        await self.adapter.ack_runtime_events(
            provider_session_id=self.provider_session_id,
            cursor=recoverable_through,
        )
        assert self.runtime_epoch is not None
        await supervision.record_provider_ack(
            self.db,
            run_id=self.run_id,
            session_id=self.provider_session_id,
            epoch=self.runtime_epoch,
            owner_id=self.owner_id,
            acked_through=recoverable_through,
        )

    async def _deliver_pending_results(self) -> None:
        assert self.runtime_session_id
        commands = await broker.pending_runtime_result_commands(
            self.db, session_id=self.runtime_session_id
        )
        await self.db.commit()
        for command in commands:
            receipt = await self.adapter.send_runtime_result(command)
            await supervision.record_runtime_result_receipt(
                self.db,
                command=command,
                receipt=receipt,
                owner_id=self.owner_id,
            )

    async def _event_loop_v2(self) -> None:
        assert self.runtime_session_id and self.provider_session_id
        exhausted = await budgets.is_exhausted(self.db, self.run_id)
        if exhausted is not None:
            await self._terminate_budget(exhausted)
        await self._recover_provider_ack()
        await self._deliver_pending_results()
        idle_polls = 0
        idle_started = time.monotonic()
        while True:
            if self.fenced:
                raise leases.StaleEpoch("protocol-v2 owner lost its lease")
            after, event_limit, byte_limit = await supervision.runtime_read_window(
                self.db, session_id=self.runtime_session_id
            )
            await self.db.commit()
            batch = await self.adapter.read_runtime_events(
                provider_session_id=self.provider_session_id,
                after=after,
                limit=event_limit,
                max_bytes=byte_limit,
            )
            if not batch.events:
                await self._charge_wall_clock()
                if time.monotonic() - idle_started >= _V2_IDLE_TIMEOUT_S:
                    raise _RunFailed("runtime_idle_timeout")
                if batch.done:
                    session = await self.db.get(
                        LabRuntimeSession, self.runtime_session_id
                    )
                    if session.status == "completed" and await supervision.runtime_final_ready(
                        self.db,
                        session_id=self.runtime_session_id,
                        require_real_result=True,
                        require_succeeded=True,
                    ):
                        await self.db.commit()
                        return
                    reason = session.last_error or (
                        "Runtime ended without a successful, fully-acked result"
                    )
                    await self.db.commit()
                    raise _RunFailed(reason)
                idle_polls += 1
                await asyncio.sleep(min(0.05 * idle_polls, 0.5))
                continue
            idle_polls = 0
            idle_started = time.monotonic()
            for event in batch.events:
                committed = await supervision.commit_runtime_event(
                    self.db, event=event, owner_id=self.owner_id
                )
                if not committed.duplicate:
                    await leases.heartbeat(
                        self.db,
                        run_id=self.run_id,
                        owner_id=self.owner_id,
                        epoch=self.epoch,
                    )
                    self.run.heartbeat_at = datetime.now(UTC)
                    await self._charge_wall_clock()
                if committed.budget_exhausted_dimension is not None:
                    await self._terminate_budget(
                        committed.budget_exhausted_dimension
                    )
                if event.event_kind == "tool_intent":
                    await self._handle_v2_intent(event, committed)
                if committed.committed_through:
                    await self.adapter.ack_runtime_events(
                        provider_session_id=self.provider_session_id,
                        cursor=committed.committed_through,
                    )
                    assert self.runtime_epoch is not None
                    await supervision.record_provider_ack(
                        self.db,
                        run_id=self.run_id,
                        session_id=self.provider_session_id,
                        epoch=self.runtime_epoch,
                        owner_id=self.owner_id,
                        acked_through=committed.committed_through,
                    )
                await self._deliver_pending_results()

    async def _handle_v2_intent(self, event, committed) -> None:
        if committed.intent_row_id is None:
            raise supervision.RuntimeProtocolConflict(
                "tool intent commit has no durable intent row"
            )
        args = dict(event.tool_args or {})
        idem = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"simverse:v2-intent:{self.runtime_session_id}:{event.intent_id}",
            )
        )
        action = None
        try:
            action = await broker.request_action(
                self.db,
                claims=self.claims,
                token=self.token,
                tool_name=event.tool_name,
                args=args,
                idempotency_key=idem,
                expected_epoch=self.epoch,
                require_remote_egress=True,
            )
        except broker.ActionDenied as exc:
            action = exc.action
        except budgets.BudgetExhausted as exc:
            await self._terminate_budget(exc.dimension)
        if action is None:
            raise supervision.RuntimeProtocolConflict(
                "Broker denial did not persist an action"
            )
        if action.status in {"executing", "reconciliation_required"}:
            while action.status in {"executing", "reconciliation_required"}:
                try:
                    action = await self._reconcile_remote_executor_action(action, args)
                except budgets.BudgetExhausted as exc:
                    await self._terminate_budget(exc.dimension)
                if action.status in {"executing", "reconciliation_required"}:
                    if self.fenced:
                        raise leases.StaleEpoch(
                            "protocol-v2 owner lost its lease during reconciliation"
                        )
                    await self._charge_wall_clock()
                    await asyncio.sleep(
                        min(settings.lab_executor_poll_interval_s, 0.5)
                    )
        if action.fencing_epoch != self.epoch:
            action = await broker.recover_action_authority(
                self.db,
                action_id=action.id,
                claims=self.claims,
                token=self.token,
                tool_name=event.tool_name,
                args=args,
                expected_epoch=self.epoch,
            )

        if action.status == "waiting_approval":
            approved = await self._await_approval(
                action,
                guard.redact_text(str((event.payload or {}).get("summary", "")))
                or "",
            )
            await self.db.refresh(action)
            if not approved and action.status == "waiting_approval":
                action = await broker.expire_pending_approval(
                    self.db,
                    action_id=action.id,
                    expected_epoch=self.epoch,
                )
        if action.status == "approved":
            try:
                executor, prepare_executor = self._select_executor(
                    event.tool_name,
                    action_id=action.id,
                    args=args,
                )
                action = await broker.execute_action(
                    self.db,
                    action_id=action.id,
                    claims=self.claims,
                    executor=executor,
                    args=args,
                    expected_epoch=self.epoch,
                    prepare_executor=prepare_executor,
                    require_remote_egress=True,
                )
            except broker.ActionDenied as exc:
                action = exc.action
            except budgets.BudgetExhausted as exc:
                await self._terminate_budget(exc.dimension)
            except (broker.ApprovalInvalid, broker.ApprovalRequired) as exc:
                raise supervision.RuntimeProtocolConflict(str(exc)) from exc
        await broker.persist_runtime_result(
            self.db,
            session_id=self.runtime_session_id,
            intent_row_id=committed.intent_row_id,
            action=action,
            owner_id=self.owner_id,
        )

    async def _collect_success_artifacts(self) -> list[ArtifactSpec]:
        assert self.runtime_session_id and self.provider_session_id
        if not await supervision.runtime_final_ready(
            self.db,
            session_id=self.runtime_session_id,
            require_real_result=True,
            require_succeeded=True,
        ):
            raise supervision.RuntimeProtocolConflict(
                "Runtime artifacts are blocked by pending or unsuccessful results"
            )
        rows = (
            await self.db.execute(
                select(LabRuntimeResult)
                .where(LabRuntimeResult.session_id == self.runtime_session_id)
                .order_by(LabRuntimeResult.created_at, LabRuntimeResult.id)
            )
        ).scalars().all()
        await self.db.commit()
        artifacts = await self.adapter.collect_artifacts_v2(
            provider_session_id=self.provider_session_id
        )
        if not artifacts:
            raise supervision.RuntimeProtocolConflict(
                "Runtime completed without a deliverable artifact"
            )
        provenance = [
            {
                "command_id": row.command_id,
                "intent_id": row.intent_id,
                "action_id": row.action_id,
                "outcome": row.outcome,
                "result_digest": row.result_digest,
            }
            for row in rows
        ]
        latest = rows[-1]
        sentinel = latest.payload_json.get("sentinel")
        for artifact in artifacts:
            if (
                isinstance(sentinel, str)
                and sentinel
                and artifact.text_md is not None
                and sentinel not in artifact.text_md
            ):
                raise supervision.RuntimeProtocolConflict(
                    "Runtime artifact omitted the Broker sentinel"
                )
            meta = dict(artifact.meta or {})
            meta.update(
                broker_result_digest=latest.result_digest,
                broker_result_provenance={
                    "command_id": latest.command_id,
                    "intent_id": latest.intent_id,
                    "action_id": latest.action_id,
                },
                broker_results=provenance,
            )
            artifact.meta = meta
        return artifacts

    def _v2_artifact_id(self, provider_artifact_id: str) -> str:
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "simverse:v2-artifact:"
                f"{self.runtime_session_id}:{provider_artifact_id}",
            )
        )

    async def _load_persisted_v2_artifacts(self) -> list[LabArtifact] | None:
        """Load an atomically completed artifact batch without Runtime I/O."""
        rows = (
            await self.db.execute(
                select(LabArtifact).where(LabArtifact.run_id == self.run_id)
            )
        ).scalars().all()
        await self.db.commit()
        marked = [
            row
            for row in rows
            if isinstance(
                (row.meta_json or {}).get(_V2_ARTIFACT_FINALIZATION_KEY),
                dict,
            )
            and (row.meta_json or {})[_V2_ARTIFACT_FINALIZATION_KEY].get(
                "runtime_session_id"
            )
            == self.runtime_session_id
        ]
        if not marked:
            return None
        count = len(marked)
        by_index: dict[int, LabArtifact] = {}
        for row in marked:
            marker = (row.meta_json or {})[_V2_ARTIFACT_FINALIZATION_KEY]
            provider_artifact_id = marker.get("provider_artifact_id")
            index = marker.get("artifact_index")
            if (
                type(marker.get("artifact_count")) is not int
                or marker["artifact_count"] != count
                or type(index) is not int
                or not 0 <= index < count
                or not isinstance(provider_artifact_id, str)
                or not provider_artifact_id
                or row.id != self._v2_artifact_id(provider_artifact_id)
                or row.task_id != self.task_id
                or row.tenant_id != self.tenant_id
                or index in by_index
            ):
                raise supervision.RuntimeProtocolConflict(
                    "persisted Runtime artifact finalization marker is invalid"
                )
            by_index[index] = row
        if set(by_index) != set(range(count)):
            raise supervision.RuntimeProtocolConflict(
                "persisted Runtime artifact batch is incomplete"
            )
        return [by_index[index] for index in range(count)]

    @staticmethod
    def _v2_artifact_matches(actual: LabArtifact, expected: LabArtifact) -> bool:
        return all(
            getattr(actual, field) == getattr(expected, field)
            for field in (
                "id",
                "run_id",
                "task_id",
                "kind",
                "title",
                "uri",
                "text_md",
                "meta_json",
                "tenant_id",
                "provider_artifact_id",
                "runtime_session_id",
                "provider_session_id",
                "producer_epoch",
                "required",
                "declared_content_type",
                "original_filename",
                "expected_sha256",
                "declared_byte_size",
                "producer_action_id",
                "provenance",
            )
        )

    async def _persist_v2_artifacts(
        self, artifacts: list[ArtifactSpec]
    ) -> list[LabArtifact]:
        from app.lab import protocol
        from app.lab.artifact_pipeline import (
            ArtifactPipelineClient,
            ArtifactPipelineError,
            ArtifactReceiptError,
        )

        if not settings.lab_artifact_pipeline_enabled:
            raise supervision.RuntimeProtocolConflict(
                "protocol-v2 cannot persist artifacts without the production pipeline"
            )
        assert self.runtime_session_id
        assert self.provider_session_id
        assert self.runtime_epoch is not None
        artifacts = sorted(
            artifacts,
            key=lambda artifact: artifact.provider_artifact_id or "",
        )
        count = len(artifacts)
        provider_ids = [artifact.provider_artifact_id for artifact in artifacts]
        if any(
            not isinstance(provider_id, str) or not provider_id
            for provider_id in provider_ids
        ) or len(set(provider_ids)) != count:
            raise supervision.RuntimeProtocolConflict(
                "Runtime artifact ids are missing or duplicated"
            )
        if not any(artifact.required for artifact in artifacts):
            raise supervision.RuntimeProtocolConflict(
                "Runtime completed without a required byte-backed artifact"
            )
        built: list[LabArtifact] = []
        for index, artifact_spec in enumerate(artifacts):
            provider_artifact_id = artifact_spec.provider_artifact_id
            assert provider_artifact_id is not None
            meta = dict(artifact_spec.meta or {})
            meta[_V2_ARTIFACT_FINALIZATION_KEY] = {
                "runtime_session_id": self.runtime_session_id,
                "artifact_count": count,
                "artifact_index": index,
                "provider_artifact_id": provider_artifact_id,
            }
            artifact = LabArtifact(
                id=self._v2_artifact_id(provider_artifact_id),
                run_id=self.run_id,
                task_id=self.task_id,
                kind=artifact_spec.kind,
                title=artifact_spec.title,
                uri=None,
                text_md=None,
                meta_json=meta,
                provider_artifact_id=provider_artifact_id,
                runtime_session_id=self.runtime_session_id,
                provider_session_id=self.provider_session_id,
                producer_epoch=self.runtime_epoch,
                required=artifact_spec.required,
                declared_content_type=artifact_spec.content_type,
                content_type=artifact_spec.content_type,
                original_filename=artifact_spec.original_filename,
                expected_sha256=artifact_spec.expected_sha256,
                declared_byte_size=artifact_spec.declared_byte_size,
                storage_status="pending_upload",
            )
            await lab_artifact_service.finalize_artifact(
                self.db,
                artifact=artifact,
                tenant_id=self.tenant_id,
                producer_action_id=artifact_spec.producer_action_id,
                scanned_clean=False,
            )
            built.append(artifact)

        existing = await self._load_persisted_v2_artifacts()
        if existing is not None:
            if len(existing) != count or any(
                not self._v2_artifact_matches(actual, expected)
                for actual, expected in zip(existing, built, strict=True)
            ):
                raise supervision.RuntimeProtocolConflict(
                    "Runtime artifact replay changed the committed batch"
                )
            persisted = existing
        else:
            await self._lock_current_authority()
            budget = await self.db.scalar(
                select(LabRunBudget)
                .where(LabRunBudget.run_id == self.run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if budget is not None:
                if (
                    budget.limit_artifact_count
                    and budget.used_artifact_count
                    + budget.reserved_artifact_count
                    + count
                    > budget.limit_artifact_count
                ):
                    budget.exhausted_dimension = "artifact_count"
                    await self.db.commit()
                    await self._terminate_budget("artifact_count")
                budget.used_artifact_count += count
            self.db.add_all(built)
            try:
                await self.db.commit()
                persisted = built
            except IntegrityError:
                await self.db.rollback()
                existing = await self._load_persisted_v2_artifacts()
                if (
                    existing is None
                    or len(existing) != count
                    or any(
                        not self._v2_artifact_matches(actual, expected)
                        for actual, expected in zip(existing, built, strict=True)
                    )
                ):
                    raise supervision.RuntimeProtocolConflict(
                        "Runtime artifact finalization raced with divergent data"
                    ) from None
                persisted = existing

        client = ArtifactPipelineClient.from_settings()
        try:
            async def acknowledge_upload(
                artifact: LabArtifact, operation: LabArtifactOperation
            ) -> None:
                if not operation.receipt_digest:
                    raise supervision.RuntimeProtocolConflict(
                        "Artifact upload is missing a receipt digest"
                    )
                ack_command = protocol.RuntimeArtifactUploadAck(
                    command_id=str(uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        "simverse:runtime-artifact-upload-ack:"
                        f"{artifact.id}:{operation.receipt_digest}",
                    )),
                    run_id=self.run_id,
                    session_id=self.provider_session_id,
                    provider_artifact_id=artifact.provider_artifact_id,
                    epoch=self.runtime_epoch,
                    upload_receipt_digest=operation.receipt_digest,
                )
                await self.adapter.ack_artifact_upload_v2(ack_command)

            for artifact, artifact_spec in zip(persisted, artifacts, strict=True):
                if artifact.kind == "link":
                    if artifact.required:
                        raise supervision.RuntimeProtocolConflict(
                            "required Runtime link was not snapshotted to bytes"
                        )
                    artifact.scan_status = "failed"
                    artifact.verification_status = "rejected"
                    artifact.scan_error_code = "external_links_not_supported"
                    await self.db.commit()
                    continue

                upload_operation = await self.db.scalar(
                    select(LabArtifactOperation)
                    .where(
                        LabArtifactOperation.artifact_id == artifact.id,
                        LabArtifactOperation.operation_type == "upload",
                    )
                    .order_by(LabArtifactOperation.created_at.desc())
                    .limit(1)
                )
                terminal_upload_receipt = bool(
                    upload_operation is not None
                    and upload_operation.state in {"failed", "quarantined"}
                    and isinstance(upload_operation.receipt_json, dict)
                    and upload_operation.receipt_json.get("receipt_type")
                    == "artifact.upload"
                    and upload_operation.receipt_digest
                )
                while (
                    artifact.storage_status == "pending_upload"
                    and not terminal_upload_receipt
                ):
                    lease, upload_operation = await client.create_upload_lease(
                        self.db,
                        artifact=artifact,
                        max_bytes=max(1, settings.lab_budget_artifact_bytes),
                    )
                    upload_command = protocol.RuntimeArtifactUploadCommand(
                        command_id=str(uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"simverse:runtime-artifact-upload:{lease.upload_id}",
                        )),
                        run_id=self.run_id,
                        session_id=self.provider_session_id,
                        provider_artifact_id=artifact.provider_artifact_id,
                        epoch=self.runtime_epoch,
                        lease=lease,
                    )
                    try:
                        runtime_receipt = await self.adapter.upload_artifact_v2(
                            upload_command
                        )
                    except RuntimeV2NonRetryableError as exc:
                        if exc.status_code != 409:
                            raise
                        await self._lock_current_authority()
                        await client.fail_upload_attempt(
                            self.db,
                            operation=upload_operation,
                            error_code="runtime_upload_lease_rejected",
                        )
                        if upload_operation.attempt >= client.upload_max_attempts:
                            raise _RunFailed(
                                "artifact_upload_retry_limit_exhausted"
                            ) from exc
                        continue
                    await self._lock_current_authority()
                    try:
                        artifact = await client.apply_upload_receipt(
                            self.db,
                            receipt_value=runtime_receipt["upload_receipt"],
                            commit=False,
                        )
                    except ArtifactReceiptError as exc:
                        await self.db.rollback()
                        raise supervision.RuntimeProtocolConflict(str(exc)) from exc
                    except ArtifactPipelineError:
                        await self.db.commit()
                        terminal_upload_receipt = True
                        break
                    break

                if upload_operation is None:
                    raise supervision.RuntimeProtocolConflict(
                        "persisted Artifact has no durable upload operation"
                    )
                if terminal_upload_receipt:
                    await acknowledge_upload(artifact, upload_operation)
                    if artifact.required:
                        raise _RunFailed(
                            "artifact_upload:"
                            f"{upload_operation.error_code or 'upload_failed'}"
                        )
                    continue
                await self._lock_current_authority()
                upload_operation = await self.db.scalar(
                    select(LabArtifactOperation)
                    .where(LabArtifactOperation.id == upload_operation.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if (
                    upload_operation is None
                    or upload_operation.state != "succeeded"
                    or not upload_operation.receipt_digest
                ):
                    raise supervision.RuntimeProtocolConflict(
                        "Artifact upload is missing a verified receipt"
                    )
                if upload_operation.accounted_at is None:
                    budget = await self.db.scalar(
                        select(LabRunBudget)
                        .where(LabRunBudget.run_id == self.run_id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if budget is not None:
                        if (
                            budget.limit_artifact_bytes
                            and budget.used_artifact_bytes
                            + budget.reserved_artifact_bytes
                            + artifact.byte_size
                            > budget.limit_artifact_bytes
                        ):
                            budget.exhausted_dimension = "artifact_bytes"
                            await self.db.commit()
                            await self._terminate_budget("artifact_bytes")
                        budget.used_artifact_bytes += artifact.byte_size
                    upload_operation.accounted_at = datetime.now(UTC)
                await self.db.commit()

                await acknowledge_upload(artifact, upload_operation)

                if artifact.scan_status == "pending":
                    await client.submit_scan(self.db, artifact=artifact)
                elif artifact.scan_status == "flagged" and artifact.required:
                    raise _RunFailed("artifact_scan_flagged")
                elif (
                    artifact.scan_status == "failed"
                    and artifact.required
                    and artifact.scan_attempts
                    >= settings.lab_artifact_scan_max_attempts
                ):
                    raise _RunFailed("artifact_scan_failed")
            return persisted
        finally:
            await client.aclose()

    def _v2_finalization_event_id(self, type: str) -> str:
        return str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"simverse:v2-finalization:{self.runtime_session_id}:{type}",
            )
        )

    async def _append_v2_finalization_event_locked(
        self, *, type: str, payload: dict
    ) -> RunEventEnvelope | None:
        """Stage one exact finalization event in the caller's lease transaction."""
        existing = (
            await self.db.execute(
                select(LabRunEvent).where(
                    LabRunEvent.run_id == self.run_id,
                    LabRunEvent.type == type,
                )
            )
        ).scalars().all()
        expected_payload = guard.redact_payload(payload or {})
        event_id = self._v2_finalization_event_id(type)
        if existing:
            if len(existing) != 1 or any(
                event.event_id != event_id
                or event.task_id != self.task_id
                or event.tenant_id != self.tenant_id
                or event.provider_event_id is not None
                or event.payload_json != expected_payload
                for event in existing
            ):
                raise supervision.RuntimeProtocolConflict(
                    f"Gateway finalization event {type} replay diverged"
                )
            return None

        envelope = RunEventEnvelope(
            event_id=event_id,
            tenant_id=self.tenant_id,
            run_id=self.run_id,
            task_id=self.task_id,
            seq=await ledger.next_seq(self.db, self.run_id),
            type=type,
            actor=self.actor,
            fencing_epoch=self.epoch,
            policy_version=self.policy_version,
            occurred_at=datetime.now(UTC),
            payload=payload or {},
        )
        appended = await ledger.append_event(
            self.db,
            envelope=envelope,
            expected_epoch=self.epoch,
            outbox_topic="lab_run_event",
            commit=False,
        )
        if appended is None:
            raise supervision.RuntimeProtocolConflict(
                f"Gateway finalization event {type} lost its exact append"
            )
        return envelope

    async def _publish_v2_step(self, envelope: RunEventEnvelope | None) -> None:
        if envelope is None or ledger.project_step(envelope) is None:
            return
        step = (
            await self.db.execute(
                select(LabRunStep)
                .where(LabRunStep.run_id == self.run_id)
                .order_by(LabRunStep.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if step is not None:
            await _ws_run_step(self.task, self.run, step)

    async def _emit_v2_finalization_once(
        self, *, type: str, payload: dict
    ) -> None:
        envelope = None
        try:
            await self._lock_current_authority()
            envelope = await self._append_v2_finalization_event_locked(
                type=type, payload=payload
            )
            await self.db.commit()
        except BaseException:
            await self.db.rollback()
            raise
        await self._publish_v2_step(envelope)

    async def _commit_v2_success(self, *, summary: str) -> bool:
        """Atomically bind task review, run success and run.completed."""
        envelope = None
        try:
            await self._lock_current_authority()
            run = await self.db.scalar(
                select(LabRun)
                .where(LabRun.id == self.run_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            task = await self.db.scalar(
                select(LabTask)
                .where(LabTask.id == self.task_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if run is None or task is None or run.task_id != task.id:
                raise supervision.RuntimeProtocolConflict(
                    "Runtime finalization task/run binding vanished"
                )

            reviewed = False
            if task.status in {"assigned", "running"}:
                reviewed = await lab_task_service.mark_review(
                    self.db,
                    task,
                    run,
                    result_summary=summary,
                    commit=False,
                )
            elif task.status in {"review", "completed", "rejected"}:
                if (
                    task.accepted_run_id != self.run_id
                    or task.result_summary_md != summary
                ):
                    raise supervision.RuntimeProtocolConflict(
                        "completed Runtime artifact does not match task review truth"
                    )
                reviewed = True
            if not reviewed:
                await self.db.rollback()
                logger.info(
                    "run %s finished but task %s no longer reviewable; "
                    "skipping completion",
                    self.run_id,
                    self.task_id,
                )
                return False
            if run.status not in {
                "queued",
                "running",
                "needs_approval",
                "succeeded",
            }:
                raise supervision.RuntimeProtocolConflict(
                    f"Runtime finalization conflicts with run state {run.status}"
                )

            envelope = await self._append_v2_finalization_event_locked(
                type="run.completed",
                payload={"summary": guard.redact_text(summary) or ""},
            )
            run.status = "succeeded"
            run.ended_at = run.ended_at or datetime.now(UTC)
            run.cost_usd_cents = self.cost_cents
            await self.db.commit()
        except BaseException:
            await self.db.rollback()
            raise

        self.run = run
        self.task = task
        await self._publish_v2_step(envelope)
        return True

    async def _succeed(self) -> None:
        """Finalize protocol-v2 success idempotently across process retries."""
        try:
            await self._lock_current_authority()
            await self.db.commit()
        except leases.StaleEpoch:
            self.fenced = True
            return

        artifacts = await self._collect_success_artifacts()
        await self._charge_wall_clock()
        persisted = await self._persist_v2_artifacts(artifacts)
        await self._stop_after_success()

        summary = (
            "; ".join(artifact.title for artifact in persisted)
            or _SUMMARY_FALLBACK
        )
        await self._emit_v2_finalization_once(
            type="artifact.emitted",
            payload={
                "count": len(persisted),
                "summary": guard.redact_text(summary) or "",
            },
        )

        if self.task.deliverable_kind == "world_change":
            await self._lock_current_authority()
            proposal = await self._ensure_world_change_proposal(summary=summary)
            await self._emit_v2_finalization_once(
                type="proposal.drafted",
                payload={"proposal_id": proposal.id, "kind": proposal.kind},
            )

        await self._charge_wall_clock()
        if await self._commit_v2_success(summary=summary):
            await _ws_task_update(self.task)

    async def _stop_after_success(self) -> None:
        # A successful Runtime session is already terminal. Durable control is
        # owned by Phase 4; P3 must not send an unauthenticated legacy stop.
        return None
