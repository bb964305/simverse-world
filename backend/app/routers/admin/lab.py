"""Admin Lab — run monitor + circuit breaker + runtime kill switch (spec §5.3, §8)."""
from datetime import datetime, UTC
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.lab_run import LabRun
from app.models.lab_task import LabTask
from app.models.user import User
from app.routers.admin.middleware import require_admin
from app.services import lab_task_service as svc

router = APIRouter(prefix="/lab", tags=["admin-lab"])


class KillSwitchBody(BaseModel):
    enabled: bool


class ArbitrationBody(BaseModel):
    decision: Literal["settle", "refund"]


class CandidateReviewBody(BaseModel):
    decision: Literal["approve", "reject"]
    note: str = ""


@router.get("/status")
async def lab_status(admin: User = Depends(require_admin)):
    """Deploy-level switch + live runtime kill-switch state."""
    from app.services.lab_readiness_service import snapshot

    return await snapshot(user_id=admin.id, is_admin=True)


@router.get("/market-candidates")
async def list_market_candidates(
    status: str | None = None,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.models.market import LabMarketCandidate
    from app.services import lab_market_candidate_service as candidates

    query = select(LabMarketCandidate).order_by(LabMarketCandidate.created_at.desc())
    if status:
        query = query.where(LabMarketCandidate.status == status)
    rows = (await db.execute(query.limit(200))).scalars().all()
    return {"candidates": [candidates.serialize(row) for row in rows]}


@router.post("/market-candidates/{candidate_id}/review")
async def review_market_candidate(
    candidate_id: str,
    body: CandidateReviewBody,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    from app.services import lab_market_candidate_service as candidates

    try:
        row = await candidates.review(
            db,
            candidate_id=candidate_id,
            reviewer_id=admin.id,
            decision=body.decision,
            note=body.note,
        )
    except candidates.CandidateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return candidates.serialize(row)


@router.post("/kill-switch")
async def set_kill_switch(body: KillSwitchBody, admin: User = Depends(require_admin),
                          db: AsyncSession = Depends(get_db)):
    """Flip the runtime kill switch live (Redis sv:lab:enabled) — no restart. On
    kill (enabled=False) also runs the supervision drill: every active run is
    cancelled, its grants revoked, its lease fenced and its task refunded (P2 exit
    'kill switch terminates all runs and revokes grants')."""
    from app.lab import set_lab_runtime_enabled, supervision
    await set_lab_runtime_enabled(body.enabled)
    resp = {"runtime_enabled": body.enabled}
    if not body.enabled:
        resp["killed"] = await supervision.kill_switch_all(db)
    return resp


@router.get("/runs")
async def list_runs(status: str | None = None, admin: User = Depends(require_admin),
                    db: AsyncSession = Depends(get_db)):
    q = select(LabRun).order_by(LabRun.created_at.desc())
    if status:
        q = q.where(LabRun.status == status)
    rows = (await db.execute(q.limit(100))).scalars().all()
    return {"runs": [svc.serialize_run(r) for r in rows]}


@router.post("/tasks/{task_id}/arbitrate")
async def arbitrate_task(
    task_id: str,
    body: ArbitrationBody,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Resolve a rejected result through the audited terminal command path."""
    try:
        task = await svc.arbitrate_result(
            db,
            task_id=task_id,
            admin_id=admin.id,
            decision=body.decision,
        )
    except svc.LabTaskError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"task": svc.serialize(task), "decision": body.decision}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, admin: User = Depends(require_admin),
                     db: AsyncSession = Depends(get_db)):
    """Circuit-breaker: cancel a run and refund the task's escrow. With the v1
    control plane on, route through ``supervision.cancel_run`` (cooperative →
    TERM → KILL escalation + grant revocation + lease fencing); the legacy
    flip-and-refund below is the flag-off fallback."""
    run = await db.get(LabRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")

    # Protocol-v2 control is a durable Runner-owned intent. The API must never
    # reconstruct a provider handle, report a stop receipt, or terminalize the
    # task before the Runtime and Executor targets have actually converged.
    if run.protocol_version == 2:
        if run.status in ("succeeded", "failed", "cancelled"):
            raise HTTPException(status_code=409, detail="run already terminal")
        from app.lab import control_plane

        request = await control_plane.submit_run_control(
            db,
            run_id=run.id,
            requested_by=admin.id,
            action="cancel",
        )
        return {
            "ok": True,
            "run_id": run.id,
            "status": request.status,
            "control_request_id": request.id,
        }

    task = await db.get(LabTask, run.task_id)
    task_needs_terminalization = (
        task is not None
        and task.status not in ("completed", "cancelled", "expired", "failed")
    )
    if run.status == "cancelled" and task_needs_terminalization:
        from app.lab import supervision
        await supervision.reconcile_cancelled_run_event(db, run_id=run_id)
        try:
            await svc.fail_task(db, task, reason=f"admin_cancel_run:{run_id}")
        except svc.LabTaskError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"run is cancelled; terminalization deferred: {exc}",
            ) from exc
        return {"ok": True, "run_id": run_id, "escalation": "already_cancelled"}
    if run.status in ("succeeded", "failed", "cancelled"):
        raise HTTPException(status_code=409, detail="run already terminal")
    from app.config import settings
    if settings.lab_agent_v1_enabled:
        from app.lab import supervision
        from app.lab.sandbox import get_adapter
        tier = await supervision.cancel_run(
            db, run_id=run_id, adapter=get_adapter(run.adapter), handle=None,
            reason="admin_cancel",
        )
        if task_needs_terminalization:
            await db.refresh(task)
            task_needs_terminalization = task.status not in (
                "completed", "cancelled", "expired", "failed"
            )
        if task_needs_terminalization:
            try:
                await svc.fail_task(db, task, reason=f"admin_cancel_run:{run_id}")
            except svc.LabTaskError as exc:
                raise HTTPException(
                    status_code=409,
                    detail=f"run cancelled; terminalization deferred: {exc}",
                ) from exc
        return {"ok": True, "run_id": run_id, "escalation": tier}

    run.status = "cancelled"
    run.ended_at = datetime.now(UTC)
    await db.commit()
    if task_needs_terminalization:
        await db.refresh(task)
        task_needs_terminalization = task.status not in (
            "completed", "cancelled", "expired", "failed"
        )
    if task_needs_terminalization:
        try:
            await svc.fail_task(db, task, reason=f"admin_cancel_run:{run_id}")
        except svc.LabTaskError as exc:
            raise HTTPException(
                status_code=409,
                detail=f"run cancelled; terminalization deferred: {exc}",
            ) from exc
    return {"ok": True, "run_id": run_id}
