"""Goal investment endpoint (E13)."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.auth_service import get_current_user
from app.services.investment_service import invest, InvestmentError

router = APIRouter(prefix="/goals", tags=["goals"])


class InvestBody(BaseModel):
    amount: int


@router.post("/{goal_id}/invest")
async def invest_in_goal(goal_id: str, body: InvestBody, request: Request, db: AsyncSession = Depends(get_db)):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    try:
        inv = await invest(db, user.id, goal_id, body.amount)
    except InvestmentError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"id": inv.id, "goal_id": inv.goal_id, "amount": inv.amount, "status": inv.status}
