"""Phase 3 observability: /metrics endpoint + Prometheus hooks + Sentry gate.

The prometheus_client registry is process-global, so tests assert on *deltas*
around the calls they make instead of absolute values.
"""
from unittest.mock import AsyncMock, MagicMock, patch

from prometheus_client import REGISTRY

from app.llm.client import chat
from app.llm.metering import record_usage
from app.observability import (
    init_sentry,
    observe_llm_call,
    observe_tick_round,
    wire_runtime_gauges,
)


def _sample(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


async def test_metrics_endpoint_exposes_domain_metrics(client):
    # Ensure at least one labeled child exists so samples (not just headers) show.
    observe_llm_call("decide", source="usage", latency_ms=120)
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    body = resp.text
    for metric in (
        "sv_llm_calls_total",
        "sv_llm_latency_seconds",
        "sv_agent_tick_round_seconds",
        "sv_ws_online_local",
        "sv_db_pool_checked_out",
    ):
        assert metric in body, f"{metric} missing from /metrics"
    # Instrumentator's own HTTP metrics ride along.
    assert "http_request" in body


async def test_observe_llm_call_counts_latency_and_parse_failures():
    calls0 = _sample("sv_llm_calls_total", {"scenario": "plan", "source": "usage"})
    fails0 = _sample("sv_llm_parse_failures_total", {"scenario": "plan"})
    lat0 = _sample("sv_llm_latency_seconds_count", {"scenario": "plan"})

    observe_llm_call("plan", source="usage", parse_ok=False, latency_ms=500)
    observe_llm_call("plan", source="usage", parse_ok=True, latency_ms=250)

    assert _sample("sv_llm_calls_total", {"scenario": "plan", "source": "usage"}) == calls0 + 2
    # only the parse_ok=False attempt counts as a parse failure
    assert _sample("sv_llm_parse_failures_total", {"scenario": "plan"}) == fails0 + 1
    assert _sample("sv_llm_latency_seconds_count", {"scenario": "plan"}) == lat0 + 2


async def test_record_usage_feeds_prometheus_even_when_persistence_off():
    # conftest disables llm_metering_enabled — the DB row is skipped but the
    # in-process metrics must still move (flag only gates persistence).
    before = _sample(
        "sv_llm_calls_total", {"scenario": "extract", "source": "estimated"}
    )
    await record_usage("extract", model="test-model", est_input_tokens=10, est_output_tokens=5)
    after = _sample(
        "sv_llm_calls_total", {"scenario": "extract", "source": "estimated"}
    )
    assert after == before + 1


async def test_chat_failure_increments_error_counter():
    before = _sample("sv_llm_errors_total", {"scenario": "unmetered"})
    stub = MagicMock()
    stub.messages.create = AsyncMock(side_effect=RuntimeError("boom"))
    with patch("app.llm.client.get_client", return_value=stub):
        try:
            await chat("sys", [{"role": "user", "content": "hi"}])
            raise AssertionError("chat() should have raised")
        except RuntimeError:
            pass
    assert _sample("sv_llm_errors_total", {"scenario": "unmetered"}) == before + 1


async def test_tick_round_histogram_observes():
    count0 = _sample("sv_agent_tick_round_seconds_count")
    sum0 = _sample("sv_agent_tick_round_seconds_sum")
    observe_tick_round(1.5)
    assert _sample("sv_agent_tick_round_seconds_count") == count0 + 1
    assert _sample("sv_agent_tick_round_seconds_sum") == sum0 + 1.5


async def test_runtime_gauges_track_ws_and_pool():
    from app.ws.manager import manager

    wire_runtime_gauges()  # idempotent — set_function replaces the callback
    base = _sample("sv_ws_online_local")
    assert base == float(len(manager.local))
    manager.local["obs-test-user"] = object()  # type: ignore[assignment]
    try:
        assert _sample("sv_ws_online_local") == base + 1
    finally:
        manager.local.pop("obs-test-user", None)
    # sqlite NullPool has no checkedout() — the gauge must degrade to 0, not raise
    assert _sample("sv_db_pool_checked_out") >= 0.0


async def test_init_sentry_is_noop_without_dsn():
    assert init_sentry("api") is False
