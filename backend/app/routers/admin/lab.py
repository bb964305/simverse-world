"""Admin Lab — run monitor + circuit breaker + runtime kill switch (spec §5.3, §8)."""
from datetime import datetime, UTC

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


@router.get("/status")
async def lab_status(admin: User = Depends(require_admin)):
    """Deploy-level switch + live runtime kill-switch state."""
    from app.config import settings
    from app.lab import is_lab_runtime_enabled
    return {
        "deploy_enabled": settings.lab_enabled,
        "runtime_enabled": await is_lab_runtime_enabled(),
        "adapter": settings.lab_adapter,
    }


@router.post("/kill-switch")
async def set_kill_switch(body: KillSwitchBody, admin: User = Depends(require_admin)):
    """Flip the runtime kill switch live (Redis sv:lab:enabled) — no restart."""
    from app.lab import set_lab_runtime_enabled
    await set_lab_runtime_enabled(body.enabled)
    return {"runtime_enabled": body.enabled}


@router.get("/runs")
async def list_runs(status: str | None = None, admin: User = Depends(require_admin),
                    db: AsyncSession = Depends(get_db)):
    q = select(LabRun).order_by(LabRun.created_at.desc())
    if status:
        q = q.where(LabRun.status == status)
    rows = (await db.execute(q.limit(100))).scalars().all()
    return {"runs": [svc.serialize_run(r) for r in rows]}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, admin: User = Depends(require_admin),
                     db: AsyncSession = Depends(get_db)):
    """Circuit-breaker: cancel a run and refund the task's escrow."""
    run = await db.get(LabRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if run.status in ("succeeded", "failed", "cancelled"):
        raise HTTPException(status_code=409, detail="run already terminal")
    run.status = "cancelled"
    run.ended_at = datetime.now(UTC)
    await db.commit()
    task = await db.get(LabTask, run.task_id)
    if task is not None and task.status not in ("completed", "cancelled", "expired", "failed"):
        await svc.fail_task(db, task, reason=f"admin_cancel_run:{run_id}")
    return {"ok": True, "run_id": run_id}
