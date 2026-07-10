"""Admin LLM usage report (P1-1).

Aggregates llm_usage rows per scenario over a trailing window so the money
sinks (COST_RESEARCH_REPORT §三) are visible. Cost sums the materialised
``cost_usd`` column, which is computed per-model at write time (see
app.llm.pricing) — an *estimate* against Anthropic list prices; the relay's
real tariff may differ.
"""
from datetime import datetime, timedelta, UTC

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.llm_usage import LLMUsage
from app.models.user import User
from app.routers.admin.middleware import require_admin

router = APIRouter(prefix="/llm-usage", tags=["admin-llm-usage"])


async def _get_usage_summary(db: AsyncSession, hours: int = 24) -> dict:
    """Per-scenario {calls, input_tokens, output_tokens, est_cost_usd} + total."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    result = await db.execute(
        select(
            LLMUsage.scenario,
            func.count(LLMUsage.id),
            func.coalesce(func.sum(LLMUsage.input_tokens), 0),
            func.coalesce(func.sum(LLMUsage.output_tokens), 0),
            func.coalesce(func.sum(LLMUsage.cost_usd), 0.0),
        )
        .where(LLMUsage.ts >= since)
        .group_by(LLMUsage.scenario)
    )

    scenarios: dict[str, dict] = {}
    total_calls = total_input = total_output = 0
    total_cost = 0.0
    for scenario, calls, input_tokens, output_tokens, cost_usd in result.all():
        scenarios[scenario] = {
            "calls": calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "est_cost_usd": round(cost_usd, 6),
        }
        total_calls += calls
        total_input += input_tokens
        total_output += output_tokens
        total_cost += cost_usd

    return {
        "hours": hours,
        "scenarios": scenarios,
        "total": {
            "calls": total_calls,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "est_cost_usd": round(total_cost, 6),
        },
    }


@router.get("/summary")
async def get_llm_usage_summary(
    hours: int = Query(24, ge=1, le=24 * 30),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    return await _get_usage_summary(db, hours=hours)
