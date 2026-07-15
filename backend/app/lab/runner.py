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
from datetime import datetime, UTC

from app.database import async_session
from app.config import settings
from app.lab import queue as lab_queue
from app.lab.sandbox import get_adapter
from app.lab.sandbox.base import RunSpec
from app.models.lab_artifact import LabArtifact
from app.models.lab_run import LabRun, LabRunStep
from app.models.lab_task import LabTask

logger = logging.getLogger(__name__)


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


async def run_one(run_id: str) -> None:
    """Execute a single queued run end-to-end. Idempotent guard: only picks up
    runs still in ``queued``."""
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
        spec = RunSpec(
            run_id=run.id, task_id=task.id, researcher_slug=run.researcher_slug,
            brief=(task.brief_md or task.title or ""), scopes=list(run.scopes_json or []),
            budget_usd=(run.budget_usd_cents or 0) / 100.0, deadline=task.deadline_at,
            egress_allowlist=list(getattr(settings, "lab_egress_allowlist", []) or []),
            secrets={}, deliverable_kind=task.deliverable_kind,
        )

        seq = 0
        cost_cents = 0
        try:
            handle = await adapter.start(spec)
            await adapter.submit_goal(handle, spec.brief, spec.scopes)
            async for ev in adapter.step_stream(handle):
                seq += 1
                cost_cents += int(ev.cost_usd_cents or 0)
                step = LabRunStep(
                    run_id=run.id, seq=seq, phase=ev.phase, tool=ev.tool,
                    summary=ev.summary, payload_json=(ev.payload or None),
                )
                db.add(step)
                run.heartbeat_at = datetime.now(UTC)
                run.cost_usd_cents = cost_cents
                await db.commit()
                await _ws_run_step(task, run, step)
                # Budget breaker (spec §5.3): hard cap on spend.
                if run.budget_usd_cents and cost_cents > run.budget_usd_cents:
                    raise RuntimeError("budget exceeded")

            artifacts = await adapter.collect_artifacts(handle)
            for a in artifacts:
                db.add(LabArtifact(
                    run_id=run.id, task_id=task.id, kind=a.kind, title=a.title,
                    uri=a.uri, text_md=a.text_md, meta_json=(a.meta or None),
                ))
            await adapter.stop(handle)

            run.status = "succeeded"
            run.ended_at = datetime.now(UTC)
            run.cost_usd_cents = cost_cents
            summary = "; ".join(a.title for a in artifacts) if artifacts else "研究完成"
            from app.services.lab_task_service import mark_review
            await mark_review(db, task, run, result_summary=summary)
            await db.commit()
            await _ws_task_update(task)
        except Exception as e:
            logger.warning("lab run %s failed: %s", run.id, e, exc_info=True)
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


async def runner_loop() -> None:
    """Long-lived consume loop for the standalone Lab Runner process."""
    logger.info("lab runner loop started (adapter default=%s)", settings.lab_adapter)
    while True:
        try:
            run_id = await lab_queue.dequeue_run(timeout=5)
            if run_id is None:
                continue
            from app.lab import is_lab_runtime_enabled
            if not await is_lab_runtime_enabled():
                # Kill switch on: put it back and back off so we don't hot-spin.
                await lab_queue.requeue_run(run_id)
                await asyncio.sleep(2.0)
                continue
            try:
                await run_one(run_id)
            finally:
                await lab_queue.ack_run(run_id)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.warning("lab runner loop error; continuing", exc_info=True)
            await asyncio.sleep(1.0)
