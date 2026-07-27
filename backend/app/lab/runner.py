"""Lab Runner core (spec §5.1).

Consumes ``sv:lab:queue``, executes a SandboxAdapter for one run, streams steps
to ``lab_run_steps`` + WS, lands artifacts, and hands the task to the state
machine (review on success, refund on failure). Decoupled from resident tick.

``run_one`` is the unit the standalone process (app/lab/main.py) and the tests
both drive; the consume loop is a thin wrapper around it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Awaitable, Callable
from datetime import datetime, UTC
from uuid import uuid4

from app.database import async_session
from app.config import settings
from app.lab import guard
from app.lab import queue as lab_queue
from app.lab.sandbox import get_adapter
from app.lab.sandbox.base import RunSpec
from app.models.lab_artifact import LabArtifact
from app.models.lab_run import LabRun, LabRunStep
from app.models.lab_task import LabTask

logger = logging.getLogger(__name__)

RunHandler = Callable[[str], Awaitable[None]]
PROTOCOL_V2_ADAPTER = "simverse_ref"
_RUNNER_OWNER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid4()}"


class ProtocolConsumerUnavailable(ValueError):
    """The selected protocol has no execution handler in this build."""


_PROTOCOL_HANDLERS: dict[int, RunHandler] = {}


def configured_protocol_version() -> int:
    """Return the one execution protocol admitted by this process."""
    return 2 if settings.lab_agent_v2_enabled else 1


def register_protocol_handler(protocol_version: int, handler: RunHandler) -> None:
    """Install a production run handler for an exact protocol version.

    P3 registers its supervised v2 handler through this boundary. Duplicate
    registration is rejected so imports cannot silently replace a live consumer.
    """
    if type(protocol_version) is not int or protocol_version not in (1, 2):
        raise ValueError(f"unsupported Lab protocol_version: {protocol_version!r}")
    if not callable(handler):
        raise TypeError("protocol handler must be callable")
    current = _PROTOCOL_HANDLERS.get(protocol_version)
    if current is not None and current is not handler:
        raise ValueError(
            f"protocol_version {protocol_version} already has a registered consumer"
        )
    _PROTOCOL_HANDLERS[protocol_version] = handler


def require_protocol_handler(
    protocol_version: int, *, adapter: str | None = None
) -> RunHandler:
    """Resolve an approved handler before admission, claim, or startup."""
    if type(protocol_version) is not int or protocol_version not in (1, 2):
        raise ProtocolConsumerUnavailable(
            f"unsupported Lab protocol_version: {protocol_version!r}"
        )
    if protocol_version == 2 and not settings.lab_runtime_v2_canary_enabled:
        raise ProtocolConsumerUnavailable(
            "Lab protocol_version 2 requires "
            "lab_runtime_v2_canary_enabled=true"
        )
    handler = _PROTOCOL_HANDLERS.get(protocol_version)
    if handler is None:
        available = ", ".join(str(version) for version in sorted(_PROTOCOL_HANDLERS))
        raise ProtocolConsumerUnavailable(
            f"Lab protocol_version {protocol_version} consumer is not ready; "
            f"this build can consume only protocol_version {available or 'none'}"
        )
    configured_adapter = (
        settings.lab_adapter if adapter is None else adapter
    )
    normalized_adapter = (configured_adapter or "").strip().lower()
    if (
        protocol_version == 2
        and normalized_adapter != PROTOCOL_V2_ADAPTER
    ):
        raise ProtocolConsumerUnavailable(
            "Lab protocol_version 2 requires "
            f"lab_adapter={PROTOCOL_V2_ADAPTER!r}; configured "
            f"{configured_adapter!r} is not an approved v2 adapter"
        )
    return handler


async def _ws_task_update(task: LabTask) -> None:
    try:
        from app.ws.manager import manager
        await manager.send(task.issuer_user_id, {
            "type": "lab_task_update",
            "task_id": task.id,
            "status": task.status,
            "run_id": task.accepted_run_id,
        })
    except Exception:
        logger.warning("lab_task_update WS send failed for %s", task.id, exc_info=True)


async def _ws_run_step(task: LabTask, run: LabRun, step: LabRunStep) -> None:
    try:
        from app.ws.manager import manager
        await manager.send(task.issuer_user_id, {
            "type": "lab_run_step",
            "task_id": task.id,
            "run_id": run.id,
            "seq": step.seq,
            "phase": step.phase,
            "tool": step.tool,
            "summary": step.summary,
        })
    except Exception:
        logger.warning("lab_run_step WS send failed for %s", run.id, exc_info=True)


async def _ws_run_approval(task: LabTask, run: LabRun, approval_id: str, summary: str) -> None:
    try:
        from app.ws.manager import manager
        await manager.send(task.issuer_user_id, {
            "type": "lab_run_approval",
            "task_id": task.id,
            "run_id": run.id,
            "approval_id": approval_id,
            "summary": summary,
        })
    except Exception:
        logger.warning("lab_run_approval WS send failed for %s", run.id, exc_info=True)


async def _await_decision(db, run: LabRun, approval_id: str) -> bool:
    """Poll the run's approvals for a resolution (set by POST /lab/runs/{id}/approval)
    up to the timeout. Timeout → deny (default-deny, spec §5.3). A non-positive
    timeout denies immediately (no human ever attached)."""
    timeout = int(settings.lab_approval_timeout_s or 0)
    waited = 0
    while waited < timeout:
        await db.refresh(run)
        for a in (run.approvals_json or []):
            if a.get("id") == approval_id and a.get("status") in ("approved", "denied"):
                return a.get("status") == "approved"
        await asyncio.sleep(1)
        waited += 1
    return False


async def _handle_approval(db, task: LabTask, run: LabRun, adapter, handle, ev) -> bool:
    """Gate a sensitive action. Financial → hard-denied immediately; other
    sensitive actions pause the run (needs_approval) for human review."""
    approval_id = (ev.approval or {}).get("id") or str(uuid4())
    verdict = guard.classify_action(ev.tool, ev.payload)
    if verdict == "deny":  # financial red line — never on the user's behalf
        await adapter.approve(handle, approval_id, False)
        return False

    run.status = "needs_approval"
    approvals = list(run.approvals_json or [])
    approvals.append({
        "id": approval_id, "tool": ev.tool,
        "summary": guard.redact_text(ev.summary), "status": "pending",
    })
    run.approvals_json = approvals
    await db.commit()
    await _ws_run_approval(task, run, approval_id, guard.redact_text(ev.summary) or "")

    decision = await _await_decision(db, run, approval_id)

    approvals = list(run.approvals_json or [])
    for a in approvals:
        if a.get("id") == approval_id:
            a["status"] = "approved" if decision else "denied"
    run.approvals_json = approvals
    run.status = "running"
    await db.commit()
    await adapter.approve(handle, approval_id, decision)
    return decision


async def run_one(run_id: str) -> None:
    """Execute a single queued run end-to-end. Idempotent guard: only picks up
    runs still in ``queued``."""
    async with async_session() as db:
        queued_run = await db.get(LabRun, run_id)
        if queued_run is None:
            logger.warning("lab run %s vanished before execution", run_id)
            return
        if queued_run.protocol_version != 1:
            raise RuntimeError(
                f"protocol v{queued_run.protocol_version} run cannot enter the v1 execution path"
            )
    if settings.lab_agent_v1_enabled:
        # v1 control-plane path (grant/policy/broker/ledger/budgets). The legacy
        # body below is preserved byte-for-byte as the rollback path (flag off).
        from app.lab import orchestrator
        return await orchestrator.run_one_v1(run_id)
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

        run.status = "running"
        run.started_at = datetime.now(UTC)
        run.heartbeat_at = datetime.now(UTC)
        task.status = "running"
        task.updated_at = datetime.now(UTC)
        await db.commit()
        await _ws_task_update(task)

        adapter = get_adapter(run.adapter)
        model_gateway_token = ""
        if run.adapter == "codex":
            from app.lab.model_policy import ModelAssignment, issue_gateway_token

            model_gateway_token = issue_gateway_token(
                tenant_id=task.issuer_user_id,
                task_id=task.id,
                run_id=run.id,
                assignment=ModelAssignment(
                    tier=run.model_tier,
                    model=run.model_name,
                    policy_version=run.model_policy_version,
                    budget_usd_cents=run.budget_usd_cents,
                    cpu_cores=run.resource_cpu_cores,
                    memory_mb=run.resource_memory_mb,
                ),
                max_model_tokens=settings.lab_budget_model_tokens,
            )
        spec = RunSpec(
            run_id=run.id, task_id=task.id, researcher_slug=run.researcher_slug,
            brief=(task.brief_md or task.title or ""), scopes=list(run.scopes_json or []),
            budget_usd=(run.budget_usd_cents or 0) / 100.0, deadline=task.deadline_at,
            egress_allowlist=list(getattr(settings, "lab_egress_allowlist", []) or []),
            secrets={}, deliverable_kind=task.deliverable_kind,
            tenant_id=task.issuer_user_id, model_tier=run.model_tier,
            model_name=run.model_name,
            model_policy_version=run.model_policy_version,
            resource_cpu_cores=run.resource_cpu_cores,
            resource_memory_mb=run.resource_memory_mb,
            model_gateway_base_url=settings.lab_model_gateway_base_url,
            model_gateway_token=model_gateway_token,
        )

        seq = 0
        cost_cents = 0
        try:
            handle = await adapter.start(spec)
            await adapter.submit_goal(handle, spec.brief, spec.scopes)
            async for ev in adapter.step_stream(handle):
                # Sensitive-action human-review breakpoint (financial → hard-deny,
                # others → pause for approval). Handled before the scope backstop
                # because an approval request is the agent *asking* permission.
                if ev.approval:
                    decision = await _handle_approval(db, task, run, adapter, handle, ev)
                    seq += 1
                    verdict_text = "已批准" if decision else "已拒绝"
                    db.add(LabRunStep(
                        run_id=run.id, seq=seq, phase="message", tool=ev.tool,
                        summary=guard.redact_text(f"敏感动作「{ev.summary}」{verdict_text}"),
                    ))
                    run.heartbeat_at = datetime.now(UTC)
                    await db.commit()
                    continue

                # Scope backstop (spec §5.3): the adapter should only expose
                # granted tools; reject any that slip through.
                if not guard.is_tool_allowed(ev.tool, list(run.scopes_json or [])):
                    raise guard.ScopeViolation(f"tool {ev.tool} outside granted scopes")

                seq += 1
                cost_cents += int(ev.cost_usd_cents or 0)
                step = LabRunStep(
                    run_id=run.id, seq=seq, phase=ev.phase, tool=ev.tool,
                    summary=guard.redact_text(ev.summary),           # 脱敏后落库/直播
                    payload_json=(guard.redact_payload(ev.payload) or None),
                )
                db.add(step)
                run.heartbeat_at = datetime.now(UTC)
                run.cost_usd_cents = cost_cents
                await db.commit()
                await _ws_run_step(task, run, step)
                # Budget breaker (spec §5.3): hard cap on spend.
                if not guard.check_budget(cost_cents, run.budget_usd_cents):
                    raise RuntimeError("budget exceeded")

            artifacts = await adapter.collect_artifacts(handle)
            for a in artifacts:
                db.add(LabArtifact(
                    run_id=run.id, task_id=task.id, kind=a.kind, title=a.title,
                    uri=a.uri, text_md=a.text_md, meta_json=(a.meta or None),
                    # Legacy flag-off path runs only the trusted Mock adapter, so
                    # its synthetic artifacts release after task completion (gap #10).
                    scan_status="clean", verification_status="verified",
                ))
            await adapter.stop(handle)

            summary = "; ".join(a.title for a in artifacts) if artifacts else "研究完成"
            from app.services.lab_task_service import mark_review
            # CAS-guarded: if the task was cancelled/finalized concurrently this is
            # a no-op (returns False) and we must NOT overwrite the cancel path's
            # run terminal or draft a proposal for a dead task (recovery plan
            # Phase 2, gap #2). The legacy path has no lease epoch to fence it, so
            # this return value is its revival guard.
            reviewed = await mark_review(
                db,
                task,
                run,
                result_summary=summary,
                commit=False,
            )
            if reviewed:
                if task.deliverable_kind == "world_change":
                    from app.services.proposal_service import create_proposal

                    await create_proposal(
                        db,
                        kind="add_lore",
                        title=f"探索产出：{task.title}"[:200],
                        rationale=(summary or "研究员在实验楼的一段冒险"),
                        patch={
                            "location_id": "experiment_building",
                            "text": (summary or "研究员在实验楼的一段冒险"),
                        },
                        origin="lab_run",
                        origin_ref=run.id,
                        author_slug=run.researcher_slug,
                        cost_sc=0,
                        commit=False,
                    )
                run.status = "succeeded"
                run.ended_at = datetime.now(UTC)
                run.cost_usd_cents = cost_cents

                await db.commit()
                await _ws_task_update(task)
            else:
                logger.info("legacy run %s finished but task %s no longer reviewable; "
                            "leaving cancel terminal intact", run.id, task.id)
        except Exception as e:
            logger.warning("lab run %s failed: %s", run.id, e, exc_info=True)
            await db.rollback()
            await db.refresh(run)
            await db.refresh(task)
            if run.status == "cancelled" or task.status == "cancelled":
                return
            run.status = "failed"
            run.ended_at = datetime.now(UTC)
            run.error = str(e)[:500]
            await db.commit()
            try:
                from app.services.lab_task_service import fail_task
                await fail_task(db, task, reason=f"run_failed:{run.id}")
                await _ws_task_update(task)
            except Exception:
                logger.error("lab task fail/refund failed for %s", task.id, exc_info=True)


async def _run_v1(run_id: str) -> None:
    # Resolve ``run_one`` at call time so normal instrumentation/patching still
    # observes the legacy handler while the registry remains stable.
    await run_one(run_id)


register_protocol_handler(1, _run_v1)


async def _run_v2(run_id: str) -> None:
    from app.lab import orchestrator

    await orchestrator.run_one_v2(run_id)


register_protocol_handler(2, _run_v2)


async def _reconcile_slots_safe() -> None:
    """Heal any concurrency slot leaked by a prior crashed Runner (re-sync the
    Redis counters to the DB's true active-run count) and refresh the content-free
    SLO gauges from ground truth. Never raises."""
    try:
        from app.database import async_session
        from app.lab import concurrency, slo
        async with async_session() as db:
            await concurrency.reconcile(db)
            await slo.collect_snapshot(db)
    except Exception:
        logger.warning("lab concurrency/slo reconcile failed", exc_info=True)


async def _reconcile_v2_processing_safe() -> None:
    """Recover processing entries whose durable owner and execution lease died."""
    try:
        from app.lab import control_plane

        async with async_session() as db:
            await control_plane.reconcile_v2_processing(db)
    except Exception:
        logger.warning("lab v2 processing reconcile failed", exc_info=True)


async def _claim_v2_queue_run(run_id: str, *, owner_id: str) -> str | None:
    from app.lab import control_plane

    async with async_session() as db:
        return await control_plane.claim_queue_run(
            db,
            run_id=run_id,
            protocol_version=2,
            owner_id=owner_id,
        )


async def _settle_v2_queue_run(
    run_id: str,
    *,
    claim_token: str,
    owner_id: str,
    disposition: str,
) -> None:
    from app.lab import control_plane

    async with async_session() as db:
        settled = await control_plane.settle_queue_claim(
            db,
            run_id=run_id,
            claim_token=claim_token,
            owner_id=owner_id,
            disposition=disposition,
        )
    if not settled:
        logger.warning("lab v2 queue claim lost before %s: %s", disposition, run_id)


async def _heartbeat_v2_queue_run(
    run_id: str, *, claim_token: str, owner_id: str
) -> None:
    from app.lab import control_plane

    interval_s = max(1.0, control_plane.QUEUE_CLAIM_S / 3)
    while True:
        await asyncio.sleep(interval_s)
        try:
            async with async_session() as db:
                alive = await control_plane.heartbeat_queue_claim(
                    db,
                    run_id=run_id,
                    claim_token=claim_token,
                    owner_id=owner_id,
                )
            if not alive:
                logger.warning("lab v2 queue claim heartbeat lost: %s", run_id)
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("lab v2 queue claim heartbeat failed: %s", run_id, exc_info=True)


async def _process_v2_with_claim_heartbeat(
    run_id: str, *, claim_token: str, owner_id: str
) -> str:
    heartbeat = asyncio.create_task(
        _heartbeat_v2_queue_run(
            run_id, claim_token=claim_token, owner_id=owner_id
        ),
        name=f"lab-v2-queue-heartbeat:{run_id}",
    )
    try:
        return await _process_run(run_id, protocol_version=2)
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)


async def _process_run(run_id: str, *, protocol_version: int) -> str:
    """Peek the dequeued run, reserve a concurrency slot, execute, release.
    Extracted from ``runner_loop`` so the admission cap is unit-testable. Returns:
    ``"ran"`` (executed), ``"full"`` (cap reached — caller requeues + backs off),
    or ``"skipped"`` (run vanished or was cancelled/terminal before pickup)."""
    handler = require_protocol_handler(protocol_version)
    from app.database import async_session
    from app.lab import concurrency
    slug = status = None
    async with async_session() as db:
        run = await db.get(LabRun, run_id)
        if run is None:
            return "skipped"
        if run.protocol_version != protocol_version:
            logger.error(
                "refusing cross-protocol queue claim for run %s: queue=v%s row=v%s",
                run_id,
                protocol_version,
                run.protocol_version,
            )
            return "skipped"
        require_protocol_handler(protocol_version, adapter=run.adapter)
        slug, status = run.researcher_slug, run.status
    resumable_states = (
        {"queued"}
        if protocol_version == 1
        else {"queued", "running", "needs_approval"}
    )
    if status not in resumable_states:
        return "skipped"  # cancelled / already terminal before a Runner picked it up
    if not await concurrency.try_reserve(researcher_slug=slug):
        return "full"
    try:
        await handler(run_id)
        return "ran"
    finally:
        await concurrency.release(researcher_slug=slug)


async def runner_loop(
    *, protocol_version: int = 1, owner_id: str | None = None
) -> None:
    """Long-lived consume loop for the standalone Lab Runner process."""
    require_protocol_handler(protocol_version)
    await lab_queue.require_legacy_queues_drained()
    queue_owner = owner_id or _RUNNER_OWNER_ID
    logger.info("lab runner loop started (adapter default=%s)", settings.lab_adapter)
    await _reconcile_slots_safe()  # heal a slot leaked by a prior crashed runner
    if protocol_version == 2:
        await _reconcile_v2_processing_safe()
    i = 0
    while True:
        try:
            run_id = await lab_queue.dequeue_run(
                protocol_version=protocol_version, timeout=5
            )
            if run_id is None:
                i += 1
                if protocol_version == 2 and i % 10 == 0:
                    await _reconcile_v2_processing_safe()
                continue
            claim_token = None
            if protocol_version == 2:
                claim_token = await _claim_v2_queue_run(
                    run_id, owner_id=queue_owner
                )
                if claim_token is None:
                    # A live durable owner already has this run. Its ACK (or the
                    # expiry reconciler) will clear duplicate Redis entries.
                    continue
            from app.lab import is_lab_runtime_enabled
            if not await is_lab_runtime_enabled():
                # Kill switch on: put it back and back off so we don't hot-spin.
                await lab_queue.requeue_run(
                    run_id, protocol_version=protocol_version
                )
                if claim_token is not None:
                    await _settle_v2_queue_run(
                        run_id,
                        claim_token=claim_token,
                        owner_id=queue_owner,
                        disposition="released",
                    )
                await asyncio.sleep(2.0)
                continue
            try:
                if claim_token is None:
                    outcome = await _process_run(
                        run_id, protocol_version=protocol_version
                    )
                else:
                    outcome = await _process_v2_with_claim_heartbeat(
                        run_id,
                        claim_token=claim_token,
                        owner_id=queue_owner,
                    )
            except Exception:
                logger.warning("lab run %s processing error; continuing", run_id, exc_info=True)
                outcome = "error"
            if outcome == "full" or (
                outcome == "error" and protocol_version == 2
            ):
                # Global/per-researcher cap reached: requeue and back off so a
                # freed slot lets it in later. A v2 processing/delivery failure
                # is also replayable from durable cursor/command state. Keep the
                # legacy v1 error disposition unchanged (ACK).
                await lab_queue.requeue_run(
                    run_id, protocol_version=protocol_version
                )
                if claim_token is not None:
                    await _settle_v2_queue_run(
                        run_id,
                        claim_token=claim_token,
                        owner_id=queue_owner,
                        disposition="released",
                    )
                await asyncio.sleep(1.0)
            else:
                await lab_queue.ack_run(
                    run_id, protocol_version=protocol_version
                )
                if claim_token is not None:
                    await _settle_v2_queue_run(
                        run_id,
                        claim_token=claim_token,
                        owner_id=queue_owner,
                        disposition="completed",
                    )
            i += 1
            if i % 50 == 0:
                await _reconcile_slots_safe()
                if protocol_version == 2:
                    await _reconcile_v2_processing_safe()
        except asyncio.CancelledError:
            break
        except Exception:
            logger.warning("lab runner loop error; continuing", exc_info=True)
            await asyncio.sleep(1.0)
