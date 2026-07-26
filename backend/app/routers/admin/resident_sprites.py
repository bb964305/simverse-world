"""Admin-only resident sprite review and publication API."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.routers.admin.middleware import require_admin
from app.schemas.resident_sprite import (
    ResidentSpriteProgressRequest,
    ResidentSpriteRejectRequest,
    ResidentSpriteReviewRequest,
    ResidentSpriteRollbackRequest,
    ResidentSpriteRunCreate,
    ResidentSpriteRunListResponse,
    ResidentSpriteRunResponse,
    VersionedSpriteAction,
)
from app.services import resident_sprite_publish_service as workflow
from app.ws.manager import manager

logger = logging.getLogger(__name__)


def require_resident_sprite_enabled() -> None:
    if not settings.resident_sprite_enabled:
        raise HTTPException(status_code=404, detail="Not found")


router = APIRouter(
    prefix="/resident-sprites",
    tags=["admin-resident-sprites"],
    dependencies=[Depends(require_resident_sprite_enabled)],
    include_in_schema=settings.resident_sprite_enabled,
)


def _raise(exc: workflow.SpriteWorkflowError):
    raise HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": exc.message})


def _dto(run) -> dict:
    data = {column.key: getattr(run, column.key) for column in run.__table__.columns}
    base = f"/admin/resident-sprites/{run.run_id}/candidate"
    data["candidate_texture_url"] = f"{base}/texture" if run.candidate_texture_path else None
    data["candidate_portrait_url"] = f"{base}/portrait" if run.candidate_portrait_path else None
    return data


@router.post("", response_model=ResidentSpriteRunResponse, status_code=201)
async def create_sprite_run(
    body: ResidentSpriteRunCreate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        run = await workflow.create_run(db, **body.model_dump())
    except workflow.SpriteWorkflowError as exc:
        _raise(exc)
    return _dto(run)


@router.get("", response_model=ResidentSpriteRunListResponse)
async def list_sprite_runs(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    resident_id: str | None = None,
    status: str | None = None,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    items, total = await workflow.list_runs(
        db, page=page, per_page=per_page, resident_id=resident_id, status=status
    )
    return {"items": [_dto(item) for item in items], "total": total, "page": page, "per_page": per_page}


@router.get("/{run_id}", response_model=ResidentSpriteRunResponse)
async def get_sprite_run(
    run_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        return _dto(await workflow.get_run(db, run_id))
    except workflow.SpriteWorkflowError as exc:
        _raise(exc)


@router.get("/{run_id}/candidate/{kind}")
async def get_sprite_candidate(
    run_id: str,
    kind: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        run = await workflow.get_run(db, run_id)
        if kind not in {"texture", "portrait"}:
            raise workflow.SpriteWorkflowError(404, "CANDIDATE_NOT_FOUND", "Candidate not found")
        raw_path = run.candidate_texture_path if kind == "texture" else run.candidate_portrait_path
        if not raw_path:
            raise workflow.SpriteWorkflowError(404, "CANDIDATE_NOT_FOUND", "Candidate not found")
        path = workflow.confined_artifact_path(raw_path, must_exist=True)
        return FileResponse(path, media_type="image/png", filename=f"{run_id}-{kind}.png")
    except workflow.SpriteWorkflowError as exc:
        _raise(exc)


@router.post("/{run_id}/progress", response_model=ResidentSpriteRunResponse)
async def progress_sprite_run(
    run_id: str,
    body: ResidentSpriteProgressRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        return _dto(await workflow.update_progress(db, await workflow.get_run(db, run_id), body))
    except workflow.SpriteWorkflowError as exc:
        _raise(exc)


@router.put("/{run_id}/review", response_model=ResidentSpriteRunResponse)
async def review_sprite_run(
    run_id: str,
    body: ResidentSpriteReviewRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        run = await workflow.get_run(db, run_id)
        return _dto(await workflow.submit_review(db, run, body, admin.id))
    except workflow.SpriteWorkflowError as exc:
        _raise(exc)


@router.post("/{run_id}/approve", response_model=ResidentSpriteRunResponse)
async def approve_sprite_run(
    run_id: str,
    body: VersionedSpriteAction,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        run = await workflow.get_run(db, run_id)
        return _dto(await workflow.approve(db, run, body.expected_version, admin.id))
    except workflow.SpriteWorkflowError as exc:
        _raise(exc)


@router.post("/{run_id}/reject", response_model=ResidentSpriteRunResponse)
async def reject_sprite_run(
    run_id: str,
    body: ResidentSpriteRejectRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        run = await workflow.get_run(db, run_id)
        return _dto(await workflow.reject(db, run, body.expected_version, admin.id, body.reason))
    except workflow.SpriteWorkflowError as exc:
        _raise(exc)


async def _broadcast_sprite(resident, run_id: str | None) -> None:
    try:
        await manager.broadcast({
            "type": "sprite_updated",
            "resident_id": resident.id,
            "slug": resident.slug,
            "name": resident.name,
            "sprite_key": resident.sprite_key,
            "sprite_url": resident.sprite_url,
            "portrait_url": resident.portrait_url,
            "content_hash": resident.sprite_content_hash,
            "run_id": run_id,
        })
    except Exception:
        logger.warning("sprite_updated broadcast failed for resident %s", resident.id, exc_info=True)


@router.post("/{run_id}/publish", response_model=ResidentSpriteRunResponse)
async def publish_sprite_run(
    run_id: str,
    body: VersionedSpriteAction,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        run, resident = await workflow.publish(
            db, await workflow.get_run(db, run_id), body.expected_version, admin.id
        )
    except workflow.SpriteWorkflowError as exc:
        _raise(exc)
    await _broadcast_sprite(resident, resident.sprite_generation_run_id)
    return _dto(run)


@router.post("/{run_id}/rollback", response_model=ResidentSpriteRunResponse)
async def rollback_sprite_run(
    run_id: str,
    body: ResidentSpriteRollbackRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        run, resident = await workflow.rollback(
            db, await workflow.get_run(db, run_id), body.expected_version, admin.id, body.reason
        )
    except workflow.SpriteWorkflowError as exc:
        _raise(exc)
    await _broadcast_sprite(resident, resident.sprite_generation_run_id)
    return _dto(run)
