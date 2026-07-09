from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.resident import Resident
from app.models.conversation import Conversation
from app.schemas.resident import ResidentListItem
from app.services.bulletin_service import list_posts, create_post, serialize

router = APIRouter(prefix="/bulletin", tags=["bulletin"])


@router.get("/posts")
async def get_posts(kind: str | None = None, cursor: str | None = None, db: AsyncSession = Depends(get_db)):
    """A4: paginated feed of bulletin posts (resident creations + notices + digests)."""
    return await list_posts(db, kind=kind, cursor=cursor)


class PostCreate(BaseModel):
    title: str
    content_md: str = ""
    kind: str = "notice"


@router.post("/posts")
async def post_notice(body: PostCreate, request: Request, db: AsyncSession = Depends(get_db)):
    """Ops announcement — require_admin."""
    from app.routers.admin.middleware import require_admin
    admin = await require_admin(request, db)
    if not body.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    post = await create_post(db, body.kind, body.title.strip(), body.content_md, author_user_id=admin.id)
    return serialize(post)


@router.get("")
async def get_bulletin(db: AsyncSession = Depends(get_db)):
    """Central plaza bulletin: top 10 hot residents, 5 newest, 24h conversation count."""
    hot_stmt = select(Resident).order_by(Resident.heat.desc()).limit(10)
    hot_result = await db.execute(hot_stmt)
    hot_residents = [ResidentListItem.model_validate(r, from_attributes=True) for r in hot_result.scalars().all()]

    new_stmt = select(Resident).order_by(Resident.created_at.desc()).limit(5)
    new_result = await db.execute(new_stmt)
    new_residents = [ResidentListItem.model_validate(r, from_attributes=True) for r in new_result.scalars().all()]

    twenty_four_hours_ago = datetime.utcnow() - timedelta(hours=24)
    count_result = await db.execute(
        select(func.count(Conversation.id)).where(Conversation.started_at >= twenty_four_hours_ago)
    )
    recent_conv_count = count_result.scalar() or 0

    return {
        "hot_residents": [r.model_dump() for r in hot_residents],
        "new_residents": [r.model_dump() for r in new_residents],
        "recent_conversations_24h": recent_conv_count,
    }
