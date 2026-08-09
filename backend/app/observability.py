"""Observability (Phase 3): Sentry init + Prometheus metrics.

Design notes
------------
- Metric objects are module-level ``prometheus_client`` primitives — cheap,
  in-process, and safe to update from any code path (they never raise on use;
  wiring failures are logged and swallowed).
- With multiple uvicorn workers each process keeps its own registry, so a
  scrape returns the numbers of whichever worker answered. Run a single
  worker, scrape each worker, or set PROMETHEUS_MULTIPROC_DIR if aggregated
  numbers matter; documented limitation for now (matches UVICORN_WORKERS=2 on
  vm212 being "close enough" for dashboards).
- ``sentry_sdk`` is imported lazily and only when ``SENTRY_DSN`` is set, so
  the dependency stays inert in dev/test.
"""

import logging

from prometheus_client import Counter, Gauge, Histogram

from app.config import settings

logger = logging.getLogger(__name__)

# --- LLM telemetry (fed from app/llm/metering.py + client.py) ---------------

LLM_CALLS = Counter(
    "sv_llm_calls_total",
    "LLM attempts recorded by metering, by scenario and usage source",
    ["scenario", "source"],
)
LLM_ERRORS = Counter(
    "sv_llm_errors_total",
    "LLM calls that raised (transport/API errors), by scenario",
    ["scenario"],
)
LLM_PARSE_FAILURES = Counter(
    "sv_llm_parse_failures_total",
    "Metered calls whose JSON output failed the balanced-brace parse (E-05)",
    ["scenario"],
)
LLM_LATENCY = Histogram(
    "sv_llm_latency_seconds",
    "LLM call latency by scenario",
    ["scenario"],
    buckets=(0.25, 0.5, 1, 2, 4, 8, 16, 32, 60),
)

# --- Agent loop --------------------------------------------------------------

TICK_ROUND_DURATION = Histogram(
    "sv_agent_tick_round_seconds",
    "Duration of one full AgentLoop tick round (all residents)",
    buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120),
)

# --- Civic public memory (town facts) ----------------------------------------

# Name deliberately carries no ``sv_`` prefix: the S2 plan and the vm212
# verification step (`curl /metrics | grep civic_facts_failopen_total`) name it
# this way. ``reason`` is the fact section that raised (mayor / policies / …).
CIVIC_FACTS_FAILOPEN = Counter(
    "civic_facts_failopen_total",
    "Town-facts snapshot reads that fell back to the previous snapshot (or gave up)",
    ["reason"],
)

# --- Runtime gauges (wired lazily to avoid import cycles) --------------------

WS_ONLINE_LOCAL = Gauge(
    "sv_ws_online_local",
    "WebSocket clients connected to this worker process",
)
DB_POOL_CHECKED_OUT = Gauge(
    "sv_db_pool_checked_out",
    "SQLAlchemy connections currently checked out of the pool",
)


def observe_llm_call(
    scenario: str,
    *,
    source: str,
    parse_ok: bool | None = None,
    latency_ms: int | None = None,
) -> None:
    """Record one LLM attempt. Never raises (metrics must not break callers)."""
    try:
        LLM_CALLS.labels(scenario=scenario, source=source).inc()
        if parse_ok is False:
            LLM_PARSE_FAILURES.labels(scenario=scenario).inc()
        if latency_ms is not None:
            LLM_LATENCY.labels(scenario=scenario).observe(latency_ms / 1000)
    except Exception:  # pragma: no cover — defensive
        logger.debug("observe_llm_call failed", exc_info=True)


def observe_llm_error(scenario: str) -> None:
    """Record one raised LLM call. Never raises."""
    try:
        LLM_ERRORS.labels(scenario=scenario).inc()
    except Exception:  # pragma: no cover — defensive
        logger.debug("observe_llm_error failed", exc_info=True)


def observe_tick_round(seconds: float) -> None:
    """Record one AgentLoop tick-round duration. Never raises."""
    try:
        TICK_ROUND_DURATION.observe(seconds)
    except Exception:  # pragma: no cover — defensive
        logger.debug("observe_tick_round failed", exc_info=True)


def wire_runtime_gauges() -> None:
    """Attach collect-time callbacks for WS online count and DB pool usage.

    Imports happen inside so app.observability itself never participates in
    an import cycle (manager/database both import config only).
    """
    try:
        from app.ws.manager import manager

        WS_ONLINE_LOCAL.set_function(lambda: len(manager.local))
    except Exception:
        logger.warning("WS online gauge not wired", exc_info=True)
    try:
        from app.database import engine

        def _checked_out() -> float:
            try:
                return float(engine.pool.checkedout())
            except Exception:  # NullPool/StaticPool on sqlite
                return 0.0

        DB_POOL_CHECKED_OUT.set_function(_checked_out)
    except Exception:
        logger.warning("DB pool gauge not wired", exc_info=True)


def init_sentry(component: str) -> bool:
    """Initialise Sentry when SENTRY_DSN is configured; no-op otherwise.

    Returns True when Sentry was actually initialised. Called from both the
    API process (app.main) and the standalone agent worker (app.agent.main);
    ``component`` distinguishes them in event tags.
    """
    if not settings.sentry_dsn:
        return False
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            environment=settings.sentry_environment,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            release=None,
        )
        sentry_sdk.set_tag("component", component)
        logger.info("Sentry initialised (component=%s)", component)
        return True
    except Exception:
        logger.warning("Sentry init failed — continuing without it", exc_info=True)
        return False
