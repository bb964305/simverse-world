"""Budget checks shared by canonical and legacy Forge pipelines."""

from sqlalchemy import func, select

from app.config import settings
from app.llm.budget import forge_blocked
from app.models.llm_usage import LLMUsage


class ForgeBudgetExceeded(RuntimeError):
    """A Forge run reached a global, per-user, or per-request hard limit."""


async def enforce_forge_budget(db, session_id: str, user_id: str) -> None:
    """Stop before the next paid call once any configured limit is reached."""
    if await forge_blocked(db, user_id):
        raise ForgeBudgetExceeded("daily LLM budget reached")

    cap = settings.budget_forge_request_usd
    if cap <= 0:
        return
    spent = float((await db.execute(
        select(func.coalesce(func.sum(LLMUsage.cost_usd), 0.0)).where(
            LLMUsage.conversation_id == session_id
        )
    )).scalar_one() or 0.0)
    if spent >= cap:
        raise ForgeBudgetExceeded(
            f"forge request budget reached: ${spent:.4f} >= ${cap}"
        )
