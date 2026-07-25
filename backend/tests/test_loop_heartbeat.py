"""P2 — background-loop heartbeats + stale alerting (engineering-health batch C).

Gap being closed: the five loops registered in ``app/main.py:86-99``
(heat / event / nightly / agent / embedding_backfill) run in exactly one
process (``run_background_tasks=true``). If one of them dies — a task that
raises out of its ``while True``, a cancelled task, a worker that never
started them — there is **no signal at all**: no log, no metric, no alert. The
budget-breaker silent-failure alerts (commit a3a32ec) are the paradigm this
follows.
"""
from datetime import datetime, timedelta, UTC

import pytest

from app.redis_client import get_redis
from app.tasks import loop_heartbeat as hb


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    monkeypatch.delenv("LOOP_HEARTBEAT_ENABLED", raising=False)
    monkeypatch.delenv("LOOP_HEARTBEAT_STALE_FACTOR", raising=False)
    monkeypatch.delenv("LOOP_HEARTBEAT_MIN_STALE_SEC", raising=False)
    monkeypatch.delenv("LOOP_HEARTBEAT_ALERT_COOLDOWN_MIN", raising=False)
    monkeypatch.delenv("LOOP_HEARTBEAT_CHECK_INTERVAL_MIN", raising=False)
    hb.reset_state_for_tests()
    yield
    hb.reset_state_for_tests()


@pytest.fixture
def sentry_calls(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(hb, "_sentry_event", lambda msg, extra=None: calls.append((msg, extra or {})))
    return calls


async def _write_beat(name: str, age_s: float) -> None:
    """Plant a heartbeat aged ``age_s`` seconds."""
    ts = (datetime.now(UTC) - timedelta(seconds=age_s)).isoformat()
    await get_redis().set(hb.heartbeat_key(name), ts)


# --------------------------------------------------------------------------- #
# registry + config                                                            #
# --------------------------------------------------------------------------- #

def test_all_five_background_loops_are_registered():
    assert set(hb.LOOP_INTERVALS) == {
        "heat", "event", "nightly", "agent", "embedding_backfill",
    }


def test_key_namespace():
    assert hb.heartbeat_key("heat") == "sv:hb:heat"


def test_threshold_is_a_multiple_of_the_loop_interval(monkeypatch):
    monkeypatch.setenv("LOOP_HEARTBEAT_STALE_FACTOR", "3")
    monkeypatch.setenv("LOOP_HEARTBEAT_MIN_STALE_SEC", "0")
    assert hb.stale_threshold_s("heat") == 3 * 3600
    assert hb.stale_threshold_s("event") == 3 * 60


def test_threshold_respects_the_floor(monkeypatch):
    """A 60s loop must not page on a single slow round."""
    monkeypatch.setenv("LOOP_HEARTBEAT_STALE_FACTOR", "3")
    monkeypatch.setenv("LOOP_HEARTBEAT_MIN_STALE_SEC", "600")
    assert hb.stale_threshold_s("event") == 600


def test_defaults_are_conservative():
    assert hb.heartbeats_enabled() is True
    assert hb.stale_threshold_s("event") >= 300
    assert hb.stale_threshold_s("nightly") >= 86400


def test_invalid_env_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("LOOP_HEARTBEAT_STALE_FACTOR", "not-a-number")
    assert hb.stale_threshold_s("heat") > 0


# --------------------------------------------------------------------------- #
# writing beats                                                                #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_beat_writes_a_timestamp():
    await hb.beat("heat", check=False)
    raw = await get_redis().get(hb.heartbeat_key("heat"))
    assert raw is not None
    assert (datetime.now(UTC) - datetime.fromisoformat(raw)).total_seconds() < 5


@pytest.mark.anyio
async def test_beat_is_a_no_op_when_disabled(monkeypatch):
    monkeypatch.setenv("LOOP_HEARTBEAT_ENABLED", "false")
    await hb.beat("heat", check=False)
    assert await get_redis().get(hb.heartbeat_key("heat")) is None


@pytest.mark.anyio
async def test_beat_never_raises_when_redis_is_down(monkeypatch):
    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(hb, "get_redis", _boom)
    await hb.beat("heat", check=False)  # must not raise


# --------------------------------------------------------------------------- #
# reading the health view                                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_snapshot_reports_fresh_never_seen_and_stale():
    await _write_beat("heat", 5)
    await _write_beat("event", 10_000)

    snap = await hb.snapshot()
    assert snap["heat"]["state"] == "ok"
    assert snap["heat"]["age_seconds"] == pytest.approx(5, abs=3)
    assert snap["event"]["state"] == "stale"
    assert snap["nightly"]["state"] == "never_seen"
    assert snap["nightly"]["age_seconds"] is None


@pytest.mark.anyio
async def test_snapshot_survives_a_corrupt_value():
    await get_redis().set(hb.heartbeat_key("heat"), "garbage")
    snap = await hb.snapshot()
    assert snap["heat"]["state"] == "never_seen"


# --------------------------------------------------------------------------- #
# alerting                                                                     #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_fresh_heartbeat_does_not_alert(sentry_calls, caplog):
    for name in hb.LOOP_INTERVALS:
        await _write_beat(name, 1)
    with caplog.at_level("WARNING"):
        assert await hb.check_stale() == []
    assert sentry_calls == []


@pytest.mark.anyio
async def test_expired_heartbeat_alerts_exactly_once(sentry_calls, caplog):
    for name in hb.LOOP_INTERVALS:
        await _write_beat(name, 1)
    await _write_beat("heat", 10 * 3600)

    with caplog.at_level("WARNING"):
        assert await hb.check_stale() == ["heat"]
        # a second sweep inside the cooldown must stay silent (no flooding)
        assert await hb.check_stale() == []
        assert await hb.check_stale() == []

    assert len(sentry_calls) == 1
    assert "heat" in sentry_calls[0][0]
    warnings = [r for r in caplog.records if "heat" in r.getMessage() and r.levelname == "WARNING"]
    assert len(warnings) == 1


@pytest.mark.anyio
async def test_cooldown_expiry_allows_a_second_alert(sentry_calls, monkeypatch):
    monkeypatch.setenv("LOOP_HEARTBEAT_ALERT_COOLDOWN_MIN", "0")
    for name in hb.LOOP_INTERVALS:
        await _write_beat(name, 1)
    await _write_beat("heat", 10 * 3600)
    assert await hb.check_stale() == ["heat"]
    assert await hb.check_stale() == ["heat"]
    assert len(sentry_calls) == 2


@pytest.mark.anyio
async def test_never_seen_loop_does_not_alert(sentry_calls):
    """A deployment that never started a loop is a config choice, not an outage."""
    assert await hb.check_stale() == []
    assert sentry_calls == []


@pytest.mark.anyio
async def test_switch_off_silences_everything(sentry_calls, monkeypatch, caplog):
    for name in hb.LOOP_INTERVALS:
        await _write_beat(name, 10 * 86400)
    monkeypatch.setenv("LOOP_HEARTBEAT_ENABLED", "false")
    with caplog.at_level("WARNING"):
        assert await hb.check_stale() == []
    assert sentry_calls == []


@pytest.mark.anyio
async def test_check_never_raises_when_redis_is_down(monkeypatch):
    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(hb, "get_redis", _boom)
    assert await hb.check_stale() == []


@pytest.mark.anyio
async def test_beat_runs_the_check_throttled(monkeypatch, sentry_calls):
    """One beat arms the sweep; the next beats inside the interval skip it."""
    monkeypatch.setenv("LOOP_HEARTBEAT_CHECK_INTERVAL_MIN", "60")
    checks = {"n": 0}

    async def _fake_check():
        checks["n"] += 1
        return []

    monkeypatch.setattr(hb, "check_stale", _fake_check)
    await hb.beat("heat")
    await hb.beat("heat")
    await hb.beat("event")
    assert checks["n"] == 1


# --------------------------------------------------------------------------- #
# wiring: every registered loop actually beats                                 #
# --------------------------------------------------------------------------- #

def test_every_background_loop_emits_a_heartbeat():
    import inspect

    from app.agent.loop import AgentLoop
    from app.tasks.embedding_backfill import embedding_backfill_loop
    from app.tasks.event_cron import event_cron_loop
    from app.tasks.heat_cron import heat_cron_loop
    from app.tasks.nightly_cron import nightly_cron_loop

    sources = {
        "heat": inspect.getsource(heat_cron_loop),
        "event": inspect.getsource(event_cron_loop),
        "nightly": inspect.getsource(nightly_cron_loop),
        "agent": inspect.getsource(AgentLoop.run),
        "embedding_backfill": inspect.getsource(embedding_backfill_loop),
    }
    for name, src in sources.items():
        assert "beat(" in src, f"{name} loop writes no heartbeat"
        assert f'"{name}"' in src, f"{name} loop beats under the wrong name"


@pytest.mark.anyio
async def test_health_loops_endpoint_is_read_only_and_reports_state(client):
    await _write_beat("heat", 1)
    await _write_beat("event", 10_000)

    resp = await client.get("/health/loops")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["loops"]["heat"]["state"] == "ok"
    assert body["loops"]["event"]["state"] == "stale"
    assert body["enabled"] is True


@pytest.mark.anyio
async def test_health_loops_is_ok_when_nothing_is_stale(client):
    for name in hb.LOOP_INTERVALS:
        await _write_beat(name, 1)
    body = (await client.get("/health/loops")).json()
    assert body["status"] == "ok"


@pytest.mark.anyio
async def test_plain_health_endpoint_is_unchanged(client):
    resp = await client.get("/health")
    assert resp.json() == {"status": "ok"}
