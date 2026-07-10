"""Admin LLM usage report — GET /admin/llm-usage/summary (P1-1)."""
import pytest
from datetime import datetime, timedelta, UTC

from app.models.llm_usage import LLMUsage


def _row(
    scenario: str, input_tokens: int, output_tokens: int, ts=None, cost_usd: float = 0.0
) -> LLMUsage:
    return LLMUsage(
        scenario=scenario,
        model="test-model",
        owner="system",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
        ts=ts or datetime.now(UTC),
    )


@pytest.mark.anyio
async def test_summary_aggregates_by_scenario(db_session):
    from app.routers.admin.llm_usage import _get_usage_summary

    db_session.add_all([
        _row("decide", 1000, 200),
        _row("decide", 3000, 400),
        _row("chat_turn", 500, 100),
        # Outside the 24h window — must be excluded
        _row("decide", 999_999, 999_999, ts=datetime.now(UTC) - timedelta(hours=48)),
    ])
    await db_session.commit()

    summary = await _get_usage_summary(db_session, hours=24)

    assert summary["hours"] == 24
    decide = summary["scenarios"]["decide"]
    assert decide["calls"] == 2
    assert decide["input_tokens"] == 4000
    assert decide["output_tokens"] == 600

    chat = summary["scenarios"]["chat_turn"]
    assert chat["calls"] == 1
    assert chat["input_tokens"] == 500
    assert chat["output_tokens"] == 100

    total = summary["total"]
    assert total["calls"] == 3
    assert total["input_tokens"] == 4500
    assert total["output_tokens"] == 700


@pytest.mark.anyio
async def test_summary_cost_sums_materialised_cost_usd(db_session):
    """est_cost_usd sums the write-time cost_usd column (see app.llm.pricing)."""
    from app.routers.admin.llm_usage import _get_usage_summary

    db_session.add_all([
        _row("plan", 2_000_000, 1_000_000, cost_usd=7.0),
        _row("plan", 100, 50, cost_usd=0.5),
        _row("decide", 100, 50, cost_usd=1.25),
    ])
    await db_session.commit()

    summary = await _get_usage_summary(db_session, hours=24)

    assert summary["scenarios"]["plan"]["est_cost_usd"] == pytest.approx(7.5)
    assert summary["scenarios"]["decide"]["est_cost_usd"] == pytest.approx(1.25)
    assert summary["total"]["est_cost_usd"] == pytest.approx(8.75)


@pytest.mark.anyio
async def test_summary_handles_default_zero_tokens(db_session):
    """Rows where the relay stripped usage (defaulted 0 tokens) must not break sums."""
    from app.routers.admin.llm_usage import _get_usage_summary

    db_session.add(LLMUsage(scenario="sbti", model="m", owner="system"))
    db_session.add(_row("sbti", 100, 50))
    await db_session.commit()

    summary = await _get_usage_summary(db_session, hours=24)
    sbti = summary["scenarios"]["sbti"]
    assert sbti["calls"] == 2
    assert sbti["input_tokens"] == 100
    assert sbti["output_tokens"] == 50


def test_summary_route_registered_behind_require_admin():
    """/admin/llm-usage/summary exists and depends on require_admin.

    FastAPI >= 0.139 includes routers lazily (_IncludedRouter), so app.routes
    no longer flattens nested APIRoutes; assert against the OpenAPI schema for
    registration and against the router's own route for the admin guard.
    """
    from app.main import app
    from app.routers.admin.llm_usage import router as llm_usage_router
    from app.routers.admin.middleware import require_admin

    # Registered on the app under the /admin prefix
    paths = app.openapi()["paths"]
    assert "get" in paths["/admin/llm-usage/summary"]

    # Guarded by require_admin
    route = next(r for r in llm_usage_router.routes if r.path == "/llm-usage/summary")
    assert "GET" in route.methods
    dependency_calls = [d.call for d in route.dependant.dependencies]
    assert require_admin in dependency_calls
