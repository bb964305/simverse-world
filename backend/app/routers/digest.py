"""Digest endpoints (A5)."""

from datetime import date as date_type

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Request

from app.database import get_db
from app.models.digest import Digest
from app.services.auth_service import get_current_user
from app.services.digest_service import serialize, generate_weekly_recap, has_real_digest_body

router = APIRouter(prefix="/digest", tags=["digest"])


@router.get("/weekly/me")
async def weekly_recap(request: Request, db: AsyncSession = Depends(get_db)):
    """E14: this week's personal recap (lazily generated, idempotent)."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    d = await generate_weekly_recap(db, user.id)
    return {"digest": serialize(d)}


@router.get("/latest")
async def latest(db: AsyncSession = Depends(get_db)):
    """E2E-08 读取侧: 只按日期倒序会把生产实测的 5 条空正文行当成"最新日报"
    原样返回给前端（比如 2026-07-28 那条），前端拿到非 null 对象就会渲染出
    一片空白，而不是「还没有日报」的空态文案。这里跳过空正文行，回退到最近
    一条有实质正文的；一条都没有则返回 None，让前端走空态分支。
    """
    rows = (await db.execute(
        select(Digest).where(Digest.scope == "village", Digest.user_id == "")
        .order_by(Digest.date.desc())
    )).scalars().all()
    d = next((row for row in rows if has_real_digest_body(row.content_md or "")), None)
    return {"digest": serialize(d) if d else None}


@router.get("")
async def by_date(date: str, db: AsyncSession = Depends(get_db)):
    try:
        day = date_type.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    d = (await db.execute(
        select(Digest).where(Digest.scope == "village", Digest.user_id == "", Digest.date == day)
    )).scalar_one_or_none()
    # 存量空正文行（同上）：行存在但没有实质正文，对前端来说应当等同于不存在。
    if d is not None and not has_real_digest_body(d.content_md or ""):
        d = None
    return {"digest": serialize(d) if d else None}
