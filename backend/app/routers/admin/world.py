"""Admin world governance (spec §7, §8): the proposal review console —
approve+apply / reject / revert. Approve always runs through the Apply engine
(no whitelist auto-apply). Only require_admin can act."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.models.world_change_proposal import WorldChangeProposal
from app.models.dynamic_location import DynamicLocation
from app.routers.admin.middleware import require_admin
from app.services import proposal_service as psvc

router = APIRouter(prefix="/world", tags=["admin-world"])


class ReviewBody(BaseModel):
    note: str = ""


@router.get("/proposals")
async def list_proposals(status: str | None = None, admin: User = Depends(require_admin),
                         db: AsyncSession = Depends(get_db)):
    q = select(WorldChangeProposal).order_by(WorldChangeProposal.created_at.desc())
    if status:
        q = q.where(WorldChangeProposal.status == status)
    rows = (await db.execute(q.limit(100))).scalars().all()
    return {"proposals": [psvc.serialize(p) for p in rows]}


@router.get("/proposals/{proposal_id}")
async def get_proposal(proposal_id: str, admin: User = Depends(require_admin),
                       db: AsyncSession = Depends(get_db)):
    p = await db.get(WorldChangeProposal, proposal_id)
    if p is None:
        raise HTTPException(status_code=404, detail="proposal not found")
    data = psvc.serialize(p)
    # Lightweight diff preview: what this proposal would add and any bounds
    # overlap it would hit (surfaced for the reviewer).
    preview: dict = {"kind": p.kind}
    if p.kind == "add_location":
        from app.lab.apply import validate_add_location
        preview["conflicts"] = validate_add_location(p.patch_json or {})
        preview["adds_location"] = (p.patch_json or {}).get("slug")
    data["preview"] = preview
    return data


@router.post("/proposals/{proposal_id}/approve")
async def approve_proposal(proposal_id: str, body: ReviewBody, admin: User = Depends(require_admin),
                           db: AsyncSession = Depends(get_db)):
    try:
        p = await psvc.approve_proposal(db, proposal_id, admin.id, body.note)
    except psvc.ProposalError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return psvc.serialize(p)


@router.post("/proposals/{proposal_id}/reject")
async def reject_proposal(proposal_id: str, body: ReviewBody, admin: User = Depends(require_admin),
                          db: AsyncSession = Depends(get_db)):
    try:
        p = await psvc.reject_proposal(db, proposal_id, admin.id, body.note)
    except psvc.ProposalError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return psvc.serialize(p)


@router.post("/proposals/{proposal_id}/revert")
async def revert_proposal(proposal_id: str, admin: User = Depends(require_admin),
                          db: AsyncSession = Depends(get_db)):
    try:
        p = await psvc.revert_proposal(db, proposal_id, admin.id)
    except psvc.ProposalError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return psvc.serialize(p)
