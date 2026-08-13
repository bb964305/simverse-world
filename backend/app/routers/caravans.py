"""Read-only caravan snapshot API; the database is the convergence source."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.caravan_lifecycle_service import current_snapshot

router = APIRouter(prefix="/caravans", tags=["caravans"])


@router.get("/current")
async def get_current_caravan(db: AsyncSession = Depends(get_db)) -> dict:
    return await current_snapshot(db)
