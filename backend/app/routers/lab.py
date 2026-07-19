"""Lab (experiment building) player-side API (spec §8).

Bearer auth mirrors commissions' ``_require_user``. Publishing is gated by the
deploy switch + the runtime kill switch; reading and settling existing tasks
stay available even when the Lab is paused (so nobody's escrow gets stuck).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.lab import acl, broker
from app.models.lab_action import LabApproval
from app.models.lab_artifact import LabArtifact
from app.models.lab_run import LabRun, LabRunStep
from app.models.lab_task import LabTask
from app.services.auth_service import get_current_user
from app.services import coin_service
from app.services import lab_artifact_service
from app.services import lab_task_service as svc

router = APIRouter(prefix="/lab", tags=["lab"])


async def _require_user(request: Request, db: AsyncSession):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    return user


async def _require_lab_enabled() -> None:
    if not settings.lab_enabled:
        raise HTTPException(status_code=503, detail="Lab is disabled")
    from app.lab import is_lab_runtime_enabled
    if not await is_lab_runtime_enabled():
        raise HTTPException(status_code=503, detail="Lab is temporarily disabled")


class CreateTaskBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    brief_md: str = ""
    scopes: list[str] = Field(default_factory=list)
    reward_sc: int = Field(gt=0)
    deliverable_kind: str = "report"
    researcher_slug: str | None = None
    deadline_hours: int | None = None


# ── researchers ───────────────────────────────────────────────────────

@router.get("/researchers")
async def list_researchers(request: Request, db: AsyncSession = Depends(get_db)):
    await _require_user(request, db)
    researchers = await svc.list_researchers(db)
    busy = await svc._busy_slugs(db)
    out = []
    for r in researchers:
        lab = (r.meta_json or {}).get("lab") or {}
        out.append({
            "slug": r.slug, "name": r.name,
            "tier": lab.get("tier"), "skills": lab.get("skills") or [],
            "busy": r.slug in busy, "avg_rating": r.avg_rating,
        })
    return {"researchers": out}


# ── tasks ─────────────────────────────────────────────────────────────

@router.post("/tasks")
async def create_task(body: CreateTaskBody, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    await _require_lab_enabled()
    try:
        task = await svc.create_task(
            db, issuer_id=user.id, title=body.title, brief=body.brief_md, scopes=body.scopes,
            reward_sc=body.reward_sc, deliverable_kind=body.deliverable_kind,
            researcher_slug=body.researcher_slug, deadline_hours=body.deadline_hours,
        )
    except svc.LabTaskError as e:
        detail = str(e)
        code = 402 if "balance" in detail else 400
        raise HTTPException(status_code=code, detail=detail)
    return svc.serialize(task)


@router.get("/tasks")
async def list_tasks(request: Request, scope: str = "mine", db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    q = select(LabTask).order_by(LabTask.created_at.desc())
    if scope == "open":
        q = q.where(LabTask.status == "funded", LabTask.researcher_slug.is_(None))
    else:
        q = q.where(LabTask.issuer_user_id == user.id)
    rows = (await db.execute(q.limit(100))).scalars().all()
    return {"tasks": [svc.serialize(t) for t in rows]}


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    task = await db.get(LabTask, task_id)
    if task is None or not acl.can_read_task(task, user_id=user.id, is_admin=user.is_admin):
        raise HTTPException(status_code=404, detail="task not found")
    run = None
    if task.accepted_run_id:
        r = await db.get(LabRun, task.accepted_run_id)
        run = svc.serialize_run(r) if r else None
    arts = (await db.execute(
        select(LabArtifact).where(LabArtifact.task_id == task_id).order_by(LabArtifact.created_at)
    )).scalars().all()
    unlocked = task.status == "completed"
    return {
        "task": svc.serialize(task),
        "run": run,
        "artifacts": [svc.serialize_artifact(a, unlocked) for a in arts],
    }


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    try:
        task = await svc.cancel_task(db, task_id, user.id)
    except svc.LabTaskError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return svc.serialize(task)


@router.post("/tasks/{task_id}/accept-result")
async def accept_result(task_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    try:
        task = await svc.accept_result(db, task_id, user.id)
    except svc.LabTaskError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return svc.serialize(task)


@router.post("/tasks/{task_id}/reject-result")
async def reject_result(task_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    try:
        task = await svc.reject_result(db, task_id, user.id)
    except svc.LabTaskError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return svc.serialize(task)


# ── runs ──────────────────────────────────────────────────────────────

@router.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    run = await db.get(LabRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    task = await db.get(LabTask, run.task_id)
    if task is None or not acl.can_read_run(run, task, user_id=user.id, is_admin=user.is_admin):
        raise HTTPException(status_code=404, detail="run not found")
    data = svc.serialize_run(run)
    if settings.lab_agent_v1_enabled:
        # v1: the approval controls are gated by the server-authoritative
        # projection (allowed_actions/can_decide/decision_scope/status), not the
        # legacy approvals_json blob. Flag off leaves the shape untouched.
        apprs = (await db.execute(
            select(LabApproval).where(LabApproval.run_id == run_id).order_by(LabApproval.created_at)
        )).scalars().all()
        data["approvals"] = [
            {**acl.approval_projection(a, task, user_id=user.id, is_admin=user.is_admin),
             "approval_id": a.id, "action_id": a.action_id, "preview": a.preview_json}
            for a in apprs
        ]
    return data


@router.get("/runs/{run_id}/steps")
async def get_run_steps(run_id: str, request: Request, after: int = 0, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    run = await db.get(LabRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    task = await db.get(LabTask, run.task_id)
    if task is None or not acl.can_read_run(run, task, user_id=user.id, is_admin=user.is_admin):
        raise HTTPException(status_code=404, detail="run not found")
    rows = (await db.execute(
        select(LabRunStep).where(LabRunStep.run_id == run_id, LabRunStep.seq > after)
        .order_by(LabRunStep.seq).limit(500)
    )).scalars().all()
    return {"steps": [svc.serialize_step(s) for s in rows]}


class ApprovalBody(BaseModel):
    approval_id: str
    decision: bool


@router.post("/runs/{run_id}/approval")
async def run_approval(run_id: str, body: ApprovalBody, request: Request, db: AsyncSession = Depends(get_db)):
    """Respond to a sensitive-action breakpoint (spec §5.3). Records the
    approve/deny decision on the run; the runner's poll picks it up and resumes
    (or the timeout default-denies)."""
    user = await _require_user(request, db)
    run = await db.get(LabRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    task = await db.get(LabTask, run.task_id)
    if task is None or not acl.can_read_run(run, task, user_id=user.id, is_admin=user.is_admin):
        raise HTTPException(status_code=404, detail="run not found")

    if settings.lab_agent_v1_enabled:
        # v1: resolve a canonical lab_approvals row through the Broker (which
        # binds the decider to the task owner/admin and flips the action). The
        # response shape is unchanged. Flag off falls through to the legacy path.
        appr = (await db.execute(
            select(LabApproval).where(
                LabApproval.id == body.approval_id,
                LabApproval.run_id == run_id,
                LabApproval.decision == "pending",
            )
        )).scalar_one_or_none()
        if appr is not None:
            try:
                await broker.decide_approval(
                    db, approval_id=appr.id, decider_user_id=user.id, approve=body.decision,
                    task_owner_id=task.issuer_user_id, is_admin=user.is_admin,
                )
            except broker.ApprovalInvalid as e:
                raise HTTPException(status_code=409, detail=str(e))
            return {"ok": True, "approval_id": body.approval_id, "decision": body.decision}

    if run.status != "needs_approval":
        raise HTTPException(status_code=409, detail="no pending approval")
    approvals = list(run.approvals_json or [])
    found = False
    for a in approvals:
        if a.get("id") == body.approval_id and a.get("status") == "pending":
            a["status"] = "approved" if body.decision else "denied"
            found = True
    if not found:
        raise HTTPException(status_code=404, detail="pending approval not found")
    run.approvals_json = approvals
    await db.commit()
    return {"ok": True, "approval_id": body.approval_id, "decision": body.decision}


# ── artifacts ─────────────────────────────────────────────────────────

@router.get("/artifacts/{artifact_id}")
async def get_artifact(artifact_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    if settings.lab_agent_v1_enabled:
        # v1: digest-verified read (V12) — a tampered row is blocked (409)
        # instead of silently served. ACL denial still reads as 404 (never
        # 403), same anti-probing rule as the legacy path below.
        try:
            art = await lab_artifact_service.verify_and_get(
                db, artifact_id=artifact_id, user_id=user.id, is_admin=user.is_admin,
            )
        except acl.AclDenied:
            raise HTTPException(status_code=404, detail="artifact not found")
        except lab_artifact_service.DigestMismatch:
            raise HTTPException(status_code=409, detail="artifact digest mismatch")
        except lab_artifact_service.ArtifactQuarantined:
            # Not scan-clean + verified — never hand back the body/URI (gap #10).
            raise HTTPException(status_code=409, detail="artifact quarantined (pending scan/verification)")
        task = await db.get(LabTask, art.task_id)
        unlocked = task is not None and task.status == "completed"
        return svc.serialize_artifact(art, unlocked)

    art = await db.get(LabArtifact, artifact_id)
    if art is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    task = await db.get(LabTask, art.task_id)
    if task is None or not acl.can_read_task(task, user_id=user.id, is_admin=user.is_admin):
        raise HTTPException(status_code=404, detail="artifact not found")
    # Anti-freeload: content unlocks only after the task is released (completed).
    unlocked = task.status == "completed"
    return svc.serialize_artifact(art, unlocked)
