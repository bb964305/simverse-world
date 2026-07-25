import pytest
from datetime import datetime, timedelta, UTC
from sqlalchemy import event
from sqlalchemy.orm.attributes import set_committed_value
from app.models.resident import Resident
from app.models.user import User
from app.models.conversation import Conversation
from app.services.heat_service import recalculate_heat, POPULAR_THRESHOLD, SLEEPING_DAYS


@pytest.fixture
async def test_resident(db_session):
    user = User(id="heat-creator", name="HeatCreator", email="heat@test.com", soul_coin_balance=0)
    db_session.add(user)
    await db_session.flush()
    r = Resident(
        slug="heat-test-r", name="热度测试居民", district="free", creator_id="heat-creator",
        status="idle", heat=0, star_rating=1, sprite_key="梅",
        tile_x=30, tile_y=65, token_cost_per_turn=1,
        ability_md="", persona_md="", soul_md="", meta_json={},
    )
    db_session.add(r)
    await db_session.commit()
    return r


@pytest.fixture
def make_conversations(db_session):
    async def _make(resident_id: str, count: int, days_ago: int = 1):
        ts = datetime.utcnow() - timedelta(days=days_ago)
        for _ in range(count):
            db_session.add(Conversation(
                user_id="heat-creator", resident_id=resident_id,
                started_at=ts, turns=1,
            ))
        await db_session.commit()
    return _make


@pytest.mark.anyio
async def test_heat_calculation(db_session, test_resident, make_conversations):
    await make_conversations(test_resident.id, count=10, days_ago=3)
    await make_conversations(test_resident.id, count=5, days_ago=10)  # outside 7-day window
    await recalculate_heat(db_session)
    await db_session.refresh(test_resident)
    assert test_resident.heat == 10


@pytest.mark.anyio
async def test_transitions_to_popular(db_session, test_resident, make_conversations):
    await make_conversations(test_resident.id, count=55, days_ago=2)
    changes = await recalculate_heat(db_session)
    await db_session.refresh(test_resident)
    assert test_resident.status == "popular"
    change = next(c for c in changes if c["slug"] == test_resident.slug)
    assert change["new_status"] == "popular"
    # resident_status broadcasts carry the E1 mood word (no mood yet → calm).
    assert change["mood_label"] == "calm"


@pytest.mark.anyio
async def test_transitions_to_sleeping(db_session, test_resident):
    test_resident.last_conversation_at = datetime.utcnow() - timedelta(days=8)
    test_resident.status = "idle"
    await db_session.commit()
    changes = await recalculate_heat(db_session)
    await db_session.refresh(test_resident)
    assert test_resident.status == "sleeping"
    assert test_resident.heat == 0


@pytest.mark.anyio
async def test_popular_drops_to_idle(db_session, test_resident, make_conversations):
    test_resident.status = "popular"
    await db_session.commit()
    await make_conversations(test_resident.id, count=5, days_ago=2)
    await recalculate_heat(db_session)
    await db_session.refresh(test_resident)
    assert test_resident.status == "idle"
    assert test_resident.heat == 5


@pytest.mark.anyio
async def test_chatting_skipped(db_session, test_resident):
    test_resident.status = "chatting"
    test_resident.last_conversation_at = datetime.utcnow() - timedelta(days=8)
    await db_session.commit()
    await recalculate_heat(db_session)
    await db_session.refresh(test_resident)
    assert test_resident.status == "chatting"


# ---------------------------------------------------------------------------
# Roadmap #12: heat_cron tz-mix regression (既有 bug, 8a0449c 起).
# On Postgres, asyncpg returns AWARE datetimes for the timezone=True
# last_conversation_at column; heat_service compared them against a NAIVE
# cutoff → TypeError killed the whole hourly recalc round. sqlite returns
# naive rows, so mimic the asyncpg shape with a load-event that swaps in an
# aware value (set_committed_value: no dirtying) for slugs marked "aware".
# ---------------------------------------------------------------------------

@pytest.fixture
def _aware_last_conversation_loader():
    def _make_aware(target):
        lca = target.last_conversation_at
        if lca is not None and lca.tzinfo is None and "aware" in target.slug:
            set_committed_value(
                target, "last_conversation_at", lca.replace(tzinfo=UTC)
            )

    def _load(target, _context):
        _make_aware(target)

    def _refresh(target, _context, _attrs):
        # instances already in the identity map re-populate via "refresh",
        # not "load" — hook both so expire_all()+select also gets the shape
        _make_aware(target)

    event.listen(Resident, "load", _load)
    event.listen(Resident, "refresh", _refresh)
    yield
    event.remove(Resident, "load", _load)
    event.remove(Resident, "refresh", _refresh)


def _tz_resident(slug: str, last_days_ago: int) -> Resident:
    return Resident(
        slug=slug, name=slug, district="free", creator_id="heat-creator",
        status="idle", heat=0, sprite_key="梅", meta_json={},
        last_conversation_at=datetime.utcnow() - timedelta(days=last_days_ago),
    )


@pytest.mark.anyio
async def test_mixed_aware_naive_last_conversation_no_crash(
    db_session, test_resident, _aware_last_conversation_loader
):
    """aware(asyncpg 形状) + naive(存量脏数据) 混在一轮里：不抛、判定都正确。"""
    stale_aware = _tz_resident("tz-aware-stale", last_days_ago=8)
    stale_naive = _tz_resident("tz-naive-stale", last_days_ago=8)
    fresh_aware = _tz_resident("tz-aware-fresh", last_days_ago=2)
    db_session.add_all([stale_aware, stale_naive, fresh_aware])
    await db_session.commit()
    db_session.expire_all()  # force reload so the aware load-event fires

    changes = await recalculate_heat(db_session)  # old code: TypeError here

    await db_session.refresh(stale_aware)
    await db_session.refresh(stale_naive)
    await db_session.refresh(fresh_aware)
    assert stale_aware.status == "sleeping"   # aware 8d ago → asleep
    assert stale_naive.status == "sleeping"   # naive 8d ago → asleep (legacy rows)
    assert fresh_aware.status == "idle"       # aware 2d ago → stays awake
    changed = {c["slug"]: c["new_status"] for c in changes}
    assert changed.get("tz-aware-stale") == "sleeping"
    assert changed.get("tz-naive-stale") == "sleeping"
    assert "tz-aware-fresh" not in changed
