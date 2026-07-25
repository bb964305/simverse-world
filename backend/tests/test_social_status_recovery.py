"""R4 — DB-side chat-lock recycling (engineering-health batch B).

Gap being closed: ``app/agent/chat.py`` locks both parties by writing
``Resident.status = "socializing"`` (chat.py:203-204) and only releases it in
the ``finally`` block (chat.py:280-286). A killed worker / OOM / container
restart never reaches that ``finally``, so the DB row stays "socializing"
forever; every later attempt to talk to that resident dies at the
``target.status in ("chatting", "socializing", "sleeping")`` pre-check
(chat.py:180-182) and resident socialising goes permanently silent.

Note the Redis-side locks in ``ws/manager.py`` do carry a TTL, but nothing
outside that module actually calls ``lock_socializing`` — the NPC<->NPC path
is guarded by the DB status alone, which is why recovery here is timestamp
driven rather than "does the Redis key still exist".
"""
from datetime import datetime, timedelta, UTC

import pytest
from sqlalchemy import select

from app.models.resident import Resident
from app.services import social_status_recovery as ssr
from app.ws.manager import SOCIAL_LOCK_TTL, SOCIALIZING_PREFIX


def _resident(slug: str, status: str = "idle", **kw) -> Resident:
    defaults = dict(
        slug=slug, name=slug.upper(), district="central_plaza", status=status,
        resident_type="npc", tile_x=70, tile_y=56,
    )
    defaults.update(kw)
    return Resident(**defaults)


async def _seed(db, *residents) -> None:
    for r in residents:
        db.add(r)
    await db.commit()


# --------------------------------------------------------------------------- #
# stamping                                                                     #
# --------------------------------------------------------------------------- #

def test_mark_socializing_sets_status_and_timestamp():
    r = _resident("ann")
    ssr.mark_socializing(r, partner_id="bo-id")
    assert r.status == "socializing"
    since = ssr.socializing_since(r)
    assert since is not None
    assert (datetime.now(UTC) - since).total_seconds() < 5
    assert (r.meta_json or {})["social_lock"]["partner"] == "bo-id"


def test_mark_socializing_preserves_other_meta_namespaces():
    r = _resident("ann", meta_json={"duty": {"key": "tavern_hub"}})
    ssr.mark_socializing(r)
    assert r.meta_json["duty"] == {"key": "tavern_hub"}
    assert "social_lock" in r.meta_json


def test_clear_socializing_resets_status_and_drops_the_stamp():
    r = _resident("ann", meta_json={"duty": {"key": "x"}})
    ssr.mark_socializing(r)
    ssr.clear_socializing(r)
    assert r.status == "idle"
    assert ssr.socializing_since(r) is None
    assert r.meta_json["duty"] == {"key": "x"}


def test_socializing_since_tolerates_garbage_stamps():
    assert ssr.socializing_since(_resident("a")) is None
    assert ssr.socializing_since(_resident("b", meta_json={"social_lock": "nope"})) is None
    assert ssr.socializing_since(
        _resident("c", meta_json={"social_lock": {"since": "not-a-date"}})
    ) is None


# --------------------------------------------------------------------------- #
# threshold / switch (env driven — config.py deliberately untouched)           #
# --------------------------------------------------------------------------- #

def test_threshold_defaults_to_the_redis_social_lock_ttl(monkeypatch):
    monkeypatch.delenv("SOCIAL_STATUS_STALE_SECONDS", raising=False)
    assert ssr.stale_threshold_s() == float(SOCIAL_LOCK_TTL)


def test_threshold_is_env_overridable(monkeypatch):
    monkeypatch.setenv("SOCIAL_STATUS_STALE_SECONDS", "120")
    assert ssr.stale_threshold_s() == 120.0
    monkeypatch.setenv("SOCIAL_STATUS_STALE_SECONDS", "garbage")
    assert ssr.stale_threshold_s() == float(SOCIAL_LOCK_TTL)


def test_recovery_switch(monkeypatch):
    monkeypatch.delenv("SOCIAL_STATUS_RECOVERY_ENABLED", raising=False)
    assert ssr.recovery_enabled() is True
    monkeypatch.setenv("SOCIAL_STATUS_RECOVERY_ENABLED", "false")
    assert ssr.recovery_enabled() is False


# --------------------------------------------------------------------------- #
# the recovery sweep                                                           #
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_crash_leftover_socializing_is_reclaimed(db_session):
    """The exact bug: a stamped lock older than the threshold goes back to idle."""
    stuck = _resident("stuck", status="socializing")
    ssr.mark_socializing(stuck, now=datetime.now(UTC) - timedelta(seconds=SOCIAL_LOCK_TTL + 60))
    await _seed(db_session, stuck)

    assert await ssr.recover_stale_socializing(db_session) == 1

    row = (await db_session.execute(
        select(Resident).where(Resident.slug == "stuck")
    )).scalar_one()
    assert row.status == "idle"
    assert ssr.socializing_since(row) is None


@pytest.mark.anyio
async def test_active_conversation_is_not_killed(db_session):
    fresh = _resident("fresh", status="socializing")
    ssr.mark_socializing(fresh, now=datetime.now(UTC) - timedelta(seconds=5))
    await _seed(db_session, fresh)

    assert await ssr.recover_stale_socializing(db_session) == 0
    row = (await db_session.execute(
        select(Resident).where(Resident.slug == "fresh")
    )).scalar_one()
    assert row.status == "socializing"


@pytest.mark.anyio
async def test_legacy_row_without_a_stamp_is_reclaimed(db_session):
    """Rows stuck by the pre-fix code carry no stamp at all — still orphans."""
    legacy = _resident("legacy", status="socializing")
    await _seed(db_session, legacy)

    assert await ssr.recover_stale_socializing(db_session) == 1
    row = (await db_session.execute(
        select(Resident).where(Resident.slug == "legacy")
    )).scalar_one()
    assert row.status == "idle"


@pytest.mark.anyio
async def test_a_live_redis_social_lock_protects_a_stale_row(db_session):
    """Cross-check: if ws.manager's TTL'd lock is still held, leave the row be."""
    from app.redis_client import get_redis

    stuck = _resident("held", status="socializing")
    ssr.mark_socializing(stuck, now=datetime.now(UTC) - timedelta(seconds=SOCIAL_LOCK_TTL + 60))
    await _seed(db_session, stuck)
    await get_redis().set(SOCIALIZING_PREFIX + stuck.id, "partner", ex=SOCIAL_LOCK_TTL)

    assert await ssr.recover_stale_socializing(db_session) == 0


@pytest.mark.anyio
async def test_other_statuses_are_left_alone(db_session):
    """Scope guard: only 'socializing' is reclaimed here."""
    await _seed(
        db_session,
        _resident("idler", status="idle"),
        _resident("sleeper", status="sleeping"),
        _resident("player_chat", status="chatting"),
    )
    assert await ssr.recover_stale_socializing(db_session) == 0
    rows = (await db_session.execute(select(Resident))).scalars().all()
    assert {r.slug: r.status for r in rows} == {
        "idler": "idle", "sleeper": "sleeping", "player_chat": "chatting",
    }


@pytest.mark.anyio
async def test_switch_off_disables_the_sweep(db_session, monkeypatch):
    monkeypatch.setenv("SOCIAL_STATUS_RECOVERY_ENABLED", "false")
    stuck = _resident("stuck", status="socializing")
    ssr.mark_socializing(stuck, now=datetime.now(UTC) - timedelta(hours=5))
    await _seed(db_session, stuck)

    assert await ssr.recover_stale_socializing(db_session) == 0


@pytest.mark.anyio
async def test_naive_timestamps_are_treated_as_utc(db_session):
    """sqlite/legacy writers may leave a naive ISO string in the stamp."""
    stuck = _resident("naive", status="socializing")
    old = (datetime.now(UTC) - timedelta(hours=3)).replace(tzinfo=None)
    stuck.meta_json = {"social_lock": {"since": old.isoformat()}}
    await _seed(db_session, stuck)

    assert await ssr.recover_stale_socializing(db_session) == 1


# --------------------------------------------------------------------------- #
# wiring                                                                       #
# --------------------------------------------------------------------------- #

def test_chat_engine_uses_the_stamping_helpers():
    """chat.py must go through mark/clear so every lock carries a timestamp."""
    import inspect

    from app.agent import chat as chat_mod

    src = inspect.getsource(chat_mod.resident_chat)
    assert "mark_socializing" in src
    assert "clear_socializing" in src
    assert 'status = "socializing"' not in src


def test_recovery_is_wired_into_heat_cron_in_its_own_block():
    """Independent try/except, fail-open, not folded into another job's block."""
    import inspect

    from app.tasks import heat_cron

    src = inspect.getsource(heat_cron.heat_cron_loop)
    assert "recover_stale_socializing" in src
    idx = src.index("recover_stale_socializing")
    assert "try:" in src[:idx]
    assert "except Exception" in src[idx:]
