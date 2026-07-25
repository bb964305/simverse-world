"""S2-1 admin offices endpoint — read-only, require_admin on every route.

Deliberately admin-only this pass: the player-facing ``GET /town/offices``
预告 (§6) belongs to the S2 frontend line — landing it here would contend
for the public /town router with other KICKOFFs.
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.routers.admin.middleware import require_admin
from app.services.office_service import OfficeService

router = APIRouter(prefix="/offices", tags=["admin-offices"])


@router.get("")
async def list_offices(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """All office rows (key, holder, institution, strategy, term window)."""
    return {"offices": await OfficeService(db).list_offices()}
