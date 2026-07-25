"""R3 — nightly cron missed-window catch-up (engineering-health batch A).

Gap being closed: ``nightly_cron_loop`` used to be a bare
``sleep(_seconds_until_next_run) -> run_nightly_jobs`` loop with no ledger of
"which anchor day did we last run for". A crash / container restart / deploy
window that straddles the 07:00 Beijing anchor silently dropped that day's
entire nightly job set, with no log and no alert.

Three states under test (fake clock + fakeredis from conftest):
1. normal on-time run          — no catch-up at boot, the scheduled run fires
2. restart across the anchor   — exactly one catch-up run at boot
3. restart on the same day     — no second run (idempotency guard)
"""
import asyncio
from datetime import datetime, timedelta, UTC

import pytest

from app.redis_client import get_redis
from app.tasks import nightly_cron


def _at(hour: int, minute: int = 0, day: int = 25) -> datetime:
    """A fixed tz-aware instant on 2026-07-<day>."""
    return datetime(2026, 7, day, hour, minute, tzinfo=UTC)


class _StopLoop(Exception):
    """Raised from the patched sleep to break the otherwise infinite loop."""


@pytest.fixture
def fake_clock(monkeypatch):
    """Pin ``now_real`` inside nightly_cron; returns a setter."""
    box = {"now": _at(9)}
    monkeypatch.setattr(nightly_cron, "now_real", lambda: box["now"])
    return box


@pytest.fixture
def recorded_jobs(monkeypatch):
    """Replace run_nightly_jobs with a recorder (loop calls the module global)."""
    calls: list[dict] = []

    async def _fake(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(nightly_cron, "run_nightly_jobs", _fake)
    return calls


@pytest.fixture
def one_shot_sleep(monkeypatch):
    """Make the loop's sleep raise so the test can exit after one iteration."""
    async def _sleep(_seconds):
        raise _StopLoop

    monkeypatch.setattr(nightly_cron.asyncio, "sleep", _sleep)


# --------------------------------------------------------------------------- #
# anchor-date helpers                                                          #
# --------------------------------------------------------------------------- #

def test_anchor_passed_is_false_before_the_anchor():
    assert nightly_cron._anchor_passed(_at(nightly_cron.RUN_HOUR - 1, 59)) is False


def test_anchor_passed_is_true_at_and_after_the_anchor():
    assert nightly_cron._anchor_passed(_at(nightly_cron.RUN_HOUR, nightly_cron.RUN_MINUTE)) is True
    assert nightly_cron._anchor_passed(_at(23, 59)) is True


def test_anchor_date_before_anchor_belongs_to_the_previous_day():
    assert nightly_cron._anchor_date(_at(3)) == "2026-07-24"
    assert nightly_cron._anchor_date(_at(9)) == "2026-07-25"


# --------------------------------------------------------------------------- #
# ledger claim (idempotency primitive)                                         #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_claim_run_date_is_once_per_day():
    assert await nightly_cron._claim_run_date("2026-07-25") is True
    assert await nightly_cron._claim_run_date("2026-07-25") is False
    assert await nightly_cron._claim_run_date("2026-07-26") is True
    assert await get_redis().get(nightly_cron._LAST_RUN_DATE_KEY) == "2026-07-26"


@pytest.mark.anyio
async def test_claim_run_date_fails_open_when_redis_is_down(monkeypatch):
    """A broken ledger must never silence the whole nightly batch."""
    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(nightly_cron, "get_redis", _boom)
    assert await nightly_cron._claim_run_date("2026-07-25") is True


# --------------------------------------------------------------------------- #
# catch-up decision                                                            #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_no_catch_up_before_today_anchor():
    assert await nightly_cron._needs_catch_up(_at(3)) is False


@pytest.mark.anyio
async def test_catch_up_needed_when_anchor_passed_and_ledger_empty():
    assert await nightly_cron._needs_catch_up(_at(9)) is True


@pytest.mark.anyio
async def test_no_catch_up_when_ledger_already_has_today():
    await get_redis().set(nightly_cron._LAST_RUN_DATE_KEY, "2026-07-25")
    assert await nightly_cron._needs_catch_up(_at(9)) is False


@pytest.mark.anyio
async def test_catch_up_needed_when_ledger_is_stale():
    await get_redis().set(nightly_cron._LAST_RUN_DATE_KEY, "2026-07-23")
    assert await nightly_cron._needs_catch_up(_at(9)) is True


@pytest.mark.anyio
async def test_needs_catch_up_does_not_fire_when_redis_is_down(monkeypatch):
    """Unknown ledger state -> stay quiet; the scheduled run still happens."""
    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(nightly_cron, "get_redis", _boom)
    assert await nightly_cron._needs_catch_up(_at(9)) is False


# --------------------------------------------------------------------------- #
# the three loop states                                                        #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_loop_state1_on_time_no_catch_up(fake_clock, recorded_jobs, one_shot_sleep):
    """Boot before the anchor: nothing runs until the scheduled wake-up."""
    fake_clock["now"] = _at(3)
    with pytest.raises(_StopLoop):
        await nightly_cron.nightly_cron_loop()
    assert recorded_jobs == []


@pytest.mark.anyio
async def test_loop_state2_restart_across_anchor_catches_up_once(
    fake_clock, recorded_jobs, one_shot_sleep, caplog
):
    """Boot after a missed anchor: exactly one catch-up run, guarded + logged."""
    fake_clock["now"] = _at(9)
    with caplog.at_level("WARNING"):
        with pytest.raises(_StopLoop):
            await nightly_cron.nightly_cron_loop()
    assert recorded_jobs == [{"once_per_day": True}]
    assert any("catching up" in r.getMessage() for r in caplog.records)


@pytest.mark.anyio
async def test_loop_state3_same_day_restart_does_not_rerun(
    fake_clock, recorded_jobs, one_shot_sleep
):
    """Ledger already holds today's anchor date -> restart runs nothing."""
    fake_clock["now"] = _at(9)
    await get_redis().set(nightly_cron._LAST_RUN_DATE_KEY, "2026-07-25")
    with pytest.raises(_StopLoop):
        await nightly_cron.nightly_cron_loop()
    assert recorded_jobs == []


@pytest.mark.anyio
async def test_loop_scheduled_run_passes_the_guard_flag(fake_clock, recorded_jobs, monkeypatch):
    """After the timed sleep the loop runs the batch with the daily guard on."""
    fake_clock["now"] = _at(3)
    state = {"slept": 0}

    async def _sleep(_seconds):
        state["slept"] += 1
        if state["slept"] > 1:
            raise _StopLoop

    monkeypatch.setattr(nightly_cron.asyncio, "sleep", _sleep)
    with pytest.raises(_StopLoop):
        await nightly_cron.nightly_cron_loop()
    assert recorded_jobs == [{"once_per_day": True}]


# --------------------------------------------------------------------------- #
# the guard inside run_nightly_jobs                                            #
# --------------------------------------------------------------------------- #

@pytest.fixture
def session_probe(monkeypatch):
    """Count how many job blocks actually opened a DB session."""
    opened = {"n": 0}

    class _DeadSession:
        async def __aenter__(self):
            opened["n"] += 1
            raise RuntimeError("no DB in this test")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(nightly_cron, "async_session", lambda: _DeadSession())
    return opened


@pytest.mark.anyio
async def test_guard_blocks_a_second_run_on_the_same_anchor_day(
    fake_clock, session_probe, monkeypatch
):
    fake_clock["now"] = _at(9)
    await nightly_cron.run_nightly_jobs(once_per_day=True)
    ran_first = session_probe["n"]
    assert ran_first > 0, "first guarded run should execute the job blocks"

    await nightly_cron.run_nightly_jobs(once_per_day=True)
    assert session_probe["n"] == ran_first, "same-day re-entry must be skipped"


@pytest.mark.anyio
async def test_guard_is_off_by_default_for_manual_calls(fake_clock, session_probe):
    """Ops/tests calling run_nightly_jobs() directly keep the old behaviour."""
    fake_clock["now"] = _at(9)
    await nightly_cron.run_nightly_jobs()
    first = session_probe["n"]
    await nightly_cron.run_nightly_jobs()
    assert session_probe["n"] > first


@pytest.mark.anyio
async def test_guard_lets_the_next_anchor_day_through(fake_clock, session_probe):
    fake_clock["now"] = _at(9)
    await nightly_cron.run_nightly_jobs(once_per_day=True)
    first = session_probe["n"]
    fake_clock["now"] = _at(9) + timedelta(days=1)
    await nightly_cron.run_nightly_jobs(once_per_day=True)
    assert session_probe["n"] > first


@pytest.mark.anyio
async def test_existing_job_blocks_are_untouched_by_the_guard():
    """Merge guard: the batch's own edits must not reorder any job block.

    S1-5 / S2-5 append new blocks to this same function in parallel; the
    ordering invariants other suites assert (opinion drift before digest, etc.)
    must survive this line's skeleton change.
    """
    import inspect

    src = inspect.getsource(nightly_cron.run_nightly_jobs)
    assert src.index("MUST run before digest") < src.index("generate_village_digest")
    assert "once_per_day" in src
    # the guard sits above every job block
    assert src.index("once_per_day") < src.index("MUST run before digest")


def test_asyncio_is_importable_for_the_patched_sleep():
    assert nightly_cron.asyncio is asyncio
