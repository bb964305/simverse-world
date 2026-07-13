"""D4 creator dashboard — GET /creator/stats aggregation tests."""
from datetime import datetime, timedelta, UTC

import pytest

from app.models.conversation import Conversation
from app.models.memory import Memory
from app.models.resident import Resident
from app.models.transaction import Transaction


def _days_ago(n: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=n)


@pytest.fixture
async def auth_headers(client):
    resp = await client.post("/auth/register", json={
        "name": "CreatorUser", "email": "creator@test.com", "password": "pass123"
    })
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture
async def user_id(client, auth_headers):
    me = await client.get("/users/me", headers=auth_headers)
    return me.json()["id"]


@pytest.fixture
async def creator_residents(db_session, user_id):
    """Two residents owned by the test user."""
    residents = [
        Resident(slug="stat-r1", name="统计一号", creator_id=user_id,
                 sprite_key="梅", star_rating=2),
        Resident(slug="stat-r2", name="统计二号", creator_id=user_id,
                 sprite_key="亚当", star_rating=1),
    ]
    for r in residents:
        db_session.add(r)
    await db_session.commit()
    return residents


@pytest.mark.anyio
async def test_creator_stats_requires_auth(client):
    resp = await client.get("/creator/stats")
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_creator_stats_empty_state(client, auth_headers):
    """No created residents → empty structure (frontend guidance state)."""
    resp = await client.get("/creator/stats", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "max-age=300"
    data = resp.json()
    assert data["residents"] == []
    assert data["daily_conversations"] == []
    assert data["daily_earnings"] == []
    assert data["weekly_ratings"] == []
    assert data["totals"] == {
        "conversations": 0, "earnings_sc": 0, "memories": 0, "avg_rating": None,
    }


@pytest.mark.anyio
async def test_creator_stats_conversation_aggregation(
    client, auth_headers, db_session, user_id, creator_residents,
):
    """Per-resident daily counts, window filtering and rating buckets."""
    r1, r2 = creator_residents
    # Another user's resident: their conversations must not leak in.
    other = Resident(slug="stat-other", name="他人居民", creator_id="someone-else")
    db_session.add(other)
    await db_session.flush()  # populate other.id before referencing it below

    convs = [
        # r1: 2 conversations today (rated 5 and 3), 1 two days ago (rated 4)
        Conversation(user_id=user_id, resident_id=r1.id, started_at=_days_ago(0), turns=3, rating=5),
        Conversation(user_id=user_id, resident_id=r1.id, started_at=_days_ago(0), turns=2, rating=3),
        Conversation(user_id=user_id, resident_id=r1.id, started_at=_days_ago(2), turns=4, rating=4),
        # r2: 1 conversation yesterday, unrated
        Conversation(user_id=user_id, resident_id=r2.id, started_at=_days_ago(1), turns=1),
        # Outside the 30-day window → excluded
        Conversation(user_id=user_id, resident_id=r1.id, started_at=_days_ago(40), turns=2, rating=1),
        # Someone else's resident → excluded
        Conversation(user_id=user_id, resident_id=other.id, started_at=_days_ago(0), turns=2, rating=1),
    ]
    for c in convs:
        db_session.add(c)
    # Memory footprint: two memories for r1 in-window, one ancient (excluded)
    db_session.add(Memory(resident_id=r1.id, type="event", content="记得你", source="chat_player"))
    db_session.add(Memory(resident_id=r1.id, type="event", content="又记得你", source="chat_player"))
    db_session.add(Memory(resident_id=r1.id, type="event", content="太久远", source="chat_player",
                          created_at=_days_ago(40)))
    await db_session.commit()

    resp = await client.get("/creator/stats", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()

    by_slug = {r["slug"]: r for r in data["residents"]}
    assert set(by_slug) == {"stat-r1", "stat-r2"}

    s1, s2 = by_slug["stat-r1"], by_slug["stat-r2"]
    assert s1["conversations_30d"] == 3
    assert s2["conversations_30d"] == 1
    # avg of ratings 5, 3, 4 (the 40-day-old rating=1 is outside the window)
    assert s1["avg_rating_30d"] == 4.0
    assert s2["avg_rating_30d"] is None
    assert s1["memories_30d"] == 2
    assert s2["memories_30d"] == 0

    # Daily series: sparse per resident, zero-filled overall
    today = datetime.now(UTC).date().isoformat()
    r1_daily = {p["date"]: p["value"] for p in s1["daily_conversations"]}
    assert r1_daily[today] == 2
    assert len(data["daily_conversations"]) == data["window_days"] == 30
    overall = {p["date"]: p["value"] for p in data["daily_conversations"]}
    assert overall[today] == 2
    assert sum(overall.values()) == 4
    assert data["totals"]["conversations"] == 4
    assert data["totals"]["memories"] == 2

    # Weekly rating buckets cover the window and average to 4.0 overall
    rated_weeks = [w for w in data["weekly_ratings"] if w["count"] > 0]
    assert sum(w["count"] for w in rated_weeks) == 3
    assert data["totals"]["avg_rating"] == 4.0


@pytest.mark.anyio
async def test_creator_stats_earnings_reconcile_with_transactions(
    client, auth_headers, db_session, user_id, creator_residents,
):
    """Earnings series must reconcile with the /profile/transactions ledger."""
    txs = [
        # Creator income (counted)
        Transaction(user_id=user_id, amount=1, reason="creator_passive:stat-r1", created_at=_days_ago(0)),
        Transaction(user_id=user_id, amount=1, reason="creator_passive:stat-r1", created_at=_days_ago(0)),
        Transaction(user_id=user_id, amount=1, reason="creator_passive:stat-r2", created_at=_days_ago(3)),
        Transaction(user_id=user_id, amount=8, reason="tip_share:post-123", created_at=_days_ago(1)),
        # Noise (not creator income → excluded)
        Transaction(user_id=user_id, amount=100, reason="signup_bonus_extra", created_at=_days_ago(0)),
        Transaction(user_id=user_id, amount=-5, reason="chat:stat-r1", created_at=_days_ago(0)),
        # The creator tipping someone else: buyer-side spend, must not deduct
        Transaction(user_id=user_id, amount=-5, reason="purchase:tip_5sc", created_at=_days_ago(1)),
        # Outside window → excluded
        Transaction(user_id=user_id, amount=1, reason="creator_passive:stat-r1", created_at=_days_ago(40)),
    ]
    for t in txs:
        db_session.add(t)
    await db_session.commit()

    resp = await client.get("/creator/stats", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()

    assert data["totals"]["earnings_sc"] == 11  # 1+1+1+8
    earnings = {p["date"]: p["value"] for p in data["daily_earnings"]}
    today = datetime.now(UTC).date()
    assert earnings[today.isoformat()] == 2
    assert earnings[(today - timedelta(days=1)).isoformat()] == 8
    assert earnings[(today - timedelta(days=3)).isoformat()] == 1

    # Per-resident attribution via creator_passive:{slug}
    by_slug = {r["slug"]: r for r in data["residents"]}
    assert by_slug["stat-r1"]["earnings_30d"] == 2
    assert by_slug["stat-r2"]["earnings_30d"] == 1

    # Reconcile against the raw ledger detail endpoint: the dashboard total
    # equals the sum of positive creator-income rows the user can see there.
    ledger = (await client.get(
        "/profile/transactions?limit=200", headers=auth_headers,
    )).json()
    since = datetime.now(UTC) - timedelta(days=29)

    def _in_window(iso: str) -> bool:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:  # sqlite round-trip may drop the offset
            dt = dt.replace(tzinfo=UTC)
        return dt >= since

    expected = sum(
        t["amount"] for t in ledger
        if t["amount"] > 0
        and _in_window(t["created_at"])
        and (t["reason"].startswith("creator_passive")
             or t["reason"].startswith("purchase:tip")
             or t["reason"].startswith("tip_share:"))
    )
    assert data["totals"]["earnings_sc"] == expected == 11


@pytest.mark.anyio
async def test_creator_stats_residents_with_no_activity(
    client, auth_headers, creator_residents,
):
    """Residents exist but have zero activity → zeroed cards, filled series."""
    resp = await client.get("/creator/stats", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["residents"]) == 2
    for r in data["residents"]:
        assert r["conversations_30d"] == 0
        assert r["avg_rating_30d"] is None
        assert r["earnings_30d"] == 0
        assert r["memories_30d"] == 0
        assert r["daily_conversations"] == []
    assert len(data["daily_conversations"]) == 30
    assert all(p["value"] == 0 for p in data["daily_conversations"])
    assert all(w["avg_rating"] is None for w in data["weekly_ratings"])
