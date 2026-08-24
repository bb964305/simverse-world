"""Lab (experiment building) player-side API (spec §8).

Bearer auth mirrors commissions' ``_require_user``. Publishing is gated by the
deploy switch + the runtime kill switch; reading and settling existing tasks
stay available even when the Lab is paused (so nobody's escrow gets stuck).
"""
from __future__ import annotations

import math
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from app.config import settings
from app.database import get_db
from app.lab import acl, broker, pricing
from app.lab.model_policy import assignment_for_reward
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


async def _require_lab_enabled(*, user_id: str, is_admin: bool) -> None:
    from app.services.lab_readiness_service import snapshot

    status = await snapshot(user_id=user_id, is_admin=is_admin)
    if status["publish_allowed"]:
        return
    if not status["beta_admitted"]:
        raise HTTPException(status_code=403, detail="Lab closed beta access required")
    raise HTTPException(status_code=503, detail="Lab publishing is unavailable")


class CreateTaskBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    brief_md: str = ""
    scopes: list[str] = Field(default_factory=list)
    reward_sc: int = Field(gt=0)
    deliverable_kind: str = "report"
    researcher_slug: str | None = None
    deadline_hours: int | None = None


class TaskQuoteBody(BaseModel):
    reward_sc: int = Field(gt=0)
    scopes: list[str] = Field(default_factory=list)


class MarketCandidateBody(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(default="", max_length=500)
    offer_type: Literal["good", "service", "contract"] = "service"
    suggested_price_sc: int = Field(default=0, ge=0, le=1000)


@router.get("/status")
async def lab_status(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    from app.services.lab_readiness_service import snapshot

    return await snapshot(user_id=user.id, is_admin=user.is_admin)


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

@router.post("/quote")
async def quote_task(body: TaskQuoteBody, request: Request, db: AsyncSession = Depends(get_db)):
    """Preview the exact server-side price, model, and resource assignment."""
    await _require_user(request, db)
    scopes = [scope for scope in body.scopes if scope in svc.ALLOWED_SCOPES]
    supported_scopes = svc.supported_scopes_for_adapter()
    unsupported_scopes = sorted(set(scopes) - set(supported_scopes))
    minimum_reward = pricing.minimum_reward_sc(scopes)
    assignment = assignment_for_reward(body.reward_sc)
    fee = math.ceil(body.reward_sc * settings.lab_platform_fee_rate)
    return {
        "reward_sc": body.reward_sc,
        "platform_fee_sc": fee,
        "total_hold_sc": body.reward_sc + fee,
        "minimum_reward_sc": minimum_reward,
        "eligible": bool(scopes) and not unsupported_scopes and body.reward_sc >= minimum_reward,
        "adapter": settings.lab_adapter,
        "available_scopes": supported_scopes,
        "unsupported_scopes": unsupported_scopes,
        "model_tier": assignment.tier,
        "model_name": assignment.model,
        "model_policy_version": assignment.policy_version,
        "resource_cpu_cores": assignment.cpu_cores,
        "resource_memory_mb": assignment.memory_mb,
        "budget_usd_cents": assignment.budget_usd_cents,
        "pro_min_reward_sc": settings.lab_pro_min_reward_sc,
    }


@router.post("/tasks")
async def create_task(body: CreateTaskBody, request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    await _require_lab_enabled(user_id=user.id, is_admin=user.is_admin)
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


@router.get("/market-candidates")
async def list_market_candidates(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await _require_user(request, db)
    from app.services import lab_market_candidate_service as candidates

    rows = await candidates.list_for_user(db, user_id=user.id)
    return {"candidates": [candidates.serialize(row) for row in rows]}


@router.post("/artifacts/{artifact_id}/market-candidate")
async def nominate_market_candidate(
    artifact_id: str,
    body: MarketCandidateBody,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await _require_user(request, db)
    from app.services import lab_market_candidate_service as candidates

    try:
        row = await candidates.nominate(
            db,
            artifact_id=artifact_id,
            user_id=user.id,
            title=body.title,
            summary=body.summary,
            offer_type=body.offer_type,
            suggested_price_sc=body.suggested_price_sc,
        )
    except candidates.CandidateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return candidates.serialize(row)


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
    try:
        art = await lab_artifact_service.get_manifest_for_user(
            db, artifact_id=artifact_id, user_id=user.id, is_admin=user.is_admin,
        )
    except acl.AclDenied:
        raise HTTPException(status_code=404, detail="artifact not found")
    task = await db.get(LabTask, art.task_id)
    unlocked = task.status == "completed"
    return svc.serialize_artifact(art, unlocked)


@router.get("/artifacts/{artifact_id}/download")
async def download_artifact(
    artifact_id: str,
    request: Request,
    disposition: Literal["attachment", "inline"] = "attachment",
    db: AsyncSession = Depends(get_db),
):
    """Serve only ACL-authorized, released, byte-verified Artifact content."""
    user = await _require_user(request, db)
    try:
        art = await lab_artifact_service.verify_and_get(
            db, artifact_id=artifact_id, user_id=user.id, is_admin=user.is_admin,
        )
    except acl.AclDenied:
        raise HTTPException(status_code=404, detail="artifact not found")
    except lab_artifact_service.DigestMismatch:
        raise HTTPException(status_code=409, detail="artifact digest mismatch")
    except lab_artifact_service.ArtifactQuarantined:
        raise HTTPException(status_code=409, detail="artifact quarantined (pending scan/verification)")

    task = await db.get(LabTask, art.task_id)
    if task is None or task.status != "completed":
        raise HTTPException(status_code=423, detail="artifact locked until the task is released")

    if art.storage_status != "legacy":
        from app.lab.artifact_download import (
            ArtifactDownloadError,
            ArtifactDownloadConfigurationError,
            ArtifactDownloadIntegrityError,
            prepare_released_artifact,
        )

        try:
            prepared = await prepare_released_artifact(art)
        except ArtifactDownloadConfigurationError:
            raise HTTPException(
                status_code=503,
                detail="released artifact reader is unavailable",
            )
        except ArtifactDownloadIntegrityError:
            raise HTTPException(
                status_code=409,
                detail="released artifact failed exact-version verification",
            )
        except ArtifactDownloadError:
            raise HTTPException(
                status_code=503,
                detail="released artifact reader is temporarily unavailable",
            )
        if (
            disposition == "inline"
            and (
                not prepared.content_type.startswith("text/")
                and prepared.content_type != "application/json"
            )
        ):
            prepared.path.unlink(missing_ok=True)
            raise HTTPException(
                status_code=415,
                detail="artifact content type cannot be rendered inline",
            )
        if (
            disposition == "inline"
            and prepared.byte_size > settings.lab_artifact_inline_max_bytes
        ):
            prepared.path.unlink(missing_ok=True)
            raise HTTPException(status_code=413, detail="artifact is too large for inline display")
        return FileResponse(
            prepared.path,
            media_type=prepared.content_type,
            filename=prepared.filename,
            content_disposition_type=disposition,
            headers={
                "X-Content-SHA256": prepared.sha256,
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            },
            background=BackgroundTask(prepared.path.unlink, missing_ok=True),
        )

    if art.kind == "text" and art.text_md is not None:
        body = art.text_md.encode("utf-8")
        if disposition == "inline" and len(body) > settings.lab_artifact_inline_max_bytes:
            raise HTTPException(status_code=413, detail="artifact is too large for inline display")
        return Response(
            content=body,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": (
                    f'{disposition}; filename="artifact-{art.id}.md"'
                ),
                "X-Content-SHA256": art.sha256 or "",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
            },
        )
    if art.uri:
        raise HTTPException(
            status_code=410,
            detail="legacy external artifacts are not available through the secure download boundary",
        )
    raise HTTPException(status_code=404, detail="no downloadable content")
