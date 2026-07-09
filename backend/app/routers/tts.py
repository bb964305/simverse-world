"""TTS endpoint (E5)."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.auth_service import get_current_user
from app.services.resident_service import get_resident_by_slug
from app.services.tts_service import synthesize, TTSError, TTSQuotaError

router = APIRouter(prefix="/tts", tags=["tts"])


class TTSRequest(BaseModel):
    resident_slug: str
    text: str


@router.post("")
async def tts(body: TTSRequest, request: Request, db: AsyncSession = Depends(get_db)):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")

    resident = await get_resident_by_slug(db, body.resident_slug)
    if not resident:
        raise HTTPException(status_code=404, detail="Resident not found")

    try:
        return await synthesize(user.id, resident, body.text)
    except TTSQuotaError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except TTSError as e:
        raise HTTPException(status_code=400, detail=str(e))
