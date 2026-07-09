"""E10 group photo — backend step: log the photo as a resident memory + a quip."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.auth_service import get_current_user
from app.services.resident_service import get_resident_by_slug

router = APIRouter(prefix="/photos", tags=["photos"])

# Resident quip templates keyed by E1 mood label.
QUIPS: dict[str, str] = {
    "excited": "太开心啦，这张一定要好好留着！",
    "content": "嗯，是个值得纪念的时刻。",
    "calm": "留个纪念，挺好的。",
    "tired": "呼，笑得我脸都僵了……不过值得。",
    "gloomy": "……谢谢你，愿意和我合影。",
    "anxious": "我、我拍得还行吧？别删掉哦。",
    "annoyed": "行吧，就这一张。",
    "furious": "哼，看在你诚意的份上，拍。",
}
DEFAULT_QUIP = "留个纪念，挺好的。"


class PhotoLog(BaseModel):
    resident_slug: str
    media_url: str | None = None


@router.post("/log")
async def log_photo(body: PhotoLog, request: Request, db: AsyncSession = Depends(get_db)):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")

    resident = await get_resident_by_slug(db, body.resident_slug)
    if not resident:
        raise HTTPException(status_code=404, detail="Resident not found")

    from app.memory.service import MemoryService
    await MemoryService(db).add_memory(
        resident.id, "event", f"我和 {user.name} 合了影，笑得很开心。",
        importance=0.5, source="photo", related_user_id=user.id,
        metadata_json={"media_url": body.media_url, "relationship_boost": 0.1},
    )

    mood = (resident.mood_json or {}).get("label")
    quip = QUIPS.get(mood, DEFAULT_QUIP)
    return {"resident_slug": resident.slug, "quip": quip}
