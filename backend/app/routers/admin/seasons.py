"""E7 admin 赛季写端 — require_admin on every route.

`seasons` 表长期 0 行，读端（/seasons/current、leaderboard）与记分端
（season_scorer → add_points）都在，唯独没有任何路径创建赛季行，于是所有
积分静默丢弃。这里补上手动写端；自动开季在 script_service.ensure_active_season。
"""
from datetime import datetime, timedelta, UTC

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.season import Season
from app.models.user import User
from app.routers.admin.middleware import require_admin
from app.services.season_service import (
    get_active_season, settle_season, _invalidate_active,
)

router = APIRouter(prefix="/seasons", tags=["admin-seasons"])


class SeasonCreate(BaseModel):
    title: str
    theme: str = ""
    days: int | None = None
    world_view: str = ""


def _serialize(s: Season) -> dict:
    return {
        "id": s.id,
        "title": s.title,
        "theme": s.theme,
        "status": s.status,
        "starts_at": s.starts_at.isoformat() if s.starts_at else None,
        "ends_at": s.ends_at.isoformat() if s.ends_at else None,
        "payload_json": s.payload_json or {},
    }


@router.get("")
async def list_seasons(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    rows = (await db.execute(
        select(Season).order_by(Season.starts_at.desc())
    )).scalars().all()
    return {"seasons": [_serialize(s) for s in rows]}


@router.post("")
async def open_season(
    body: SeasonCreate,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """开一季。两季并行会让 add_points 的归属变得不确定，所以拒绝。"""
    if not body.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    if await get_active_season(db) is not None:
        raise HTTPException(status_code=400,
                            detail="a season is already active; settle it first")
    days = body.days if body.days is not None else settings.season_length_days
    if days <= 0:
        raise HTTPException(status_code=400, detail="days must be positive")

    now = datetime.now(UTC)
    season = Season(
        title=body.title.strip(), theme=body.theme, status="active",
        starts_at=now, ends_at=now + timedelta(days=days),
        payload_json={"world_view": body.world_view} if body.world_view else {},
    )
    db.add(season)
    await db.commit()
    await db.refresh(season)
    # 60s 缓存不打掉的话，新赛季最长 1 分钟不可见、记分继续丢。
    _invalidate_active()
    return _serialize(season)


@router.post("/{season_id}/settle")
async def settle(
    season_id: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    season = await db.get(Season, season_id)
    if season is None:
        raise HTTPException(status_code=404, detail="Season not found")
    payload = await settle_season(db, season)
    _invalidate_active()
    return {"season": _serialize(season), "payload": payload}
