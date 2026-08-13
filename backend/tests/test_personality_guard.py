import pytest
from datetime import datetime, UTC, timedelta
from unittest.mock import AsyncMock, patch, MagicMock
from app.personality.guard import PersonalityGuard
from app.models.personality_history import PersonalityHistory
from app.models.resident import Resident
from app.models.memory import Memory


@pytest.fixture
async def guard_resident(db_session):
    r = Resident(
        id="guard-test-res",
        slug="guard-test-res",
        name="GuardResident",
        district="engineering",
        status="idle",
        creator_id="test-user-id",
        ability_md="Test",
        persona_md="Test",
        soul_md="Test",
        meta_json={"sbti": {
            "type": "CTRL",
            "type_name": "拿捏者",
            "dimensions": {
                "S1": "H", "S2": "H", "S3": "H",
                "E1": "H", "E2": "M", "E3": "H",
                "A1": "M", "A2": "H", "A3": "H",
                "Ac1": "H", "Ac2": "H", "Ac3": "H",
                "So1": "M", "So2": "H", "So3": "M",
            },
        }},
    )
    db_session.add(r)
    await db_session.commit()
    return r


# ── can_drift tests ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_can_drift_true_when_no_history(db_session, guard_resident):
    """No history → drift is allowed."""
    guard = PersonalityGuard()
    result = await guard.can_drift(guard_resident.id, db_session)
    assert result is True


@pytest.mark.anyio
async def test_can_drift_false_when_recent_drift(db_session, guard_resident):
    """If there was a drift in the last 14 memories, deny."""
    # Seed 10 event memories (below MIN_DRIFT_INTERVAL=15)
    for i in range(10):
        mem = Memory(
            resident_id=guard_resident.id,
            type="event",
            content=f"Event {i}",
            importance=0.5,
            source="chat_player",
        )
        db_session.add(mem)

    # Add a drift history entry dated now
    history = PersonalityHistory(
        resident_id=guard_resident.id,
        trigger_type="drift",
        changes_json={"S1": {"from": "M", "to": "H"}},
        old_type="CTRL",
        new_type="CTRL",
        reason="Previous drift",
        created_at=datetime.now(UTC),
    )
    db_session.add(history)
    await db_session.commit()

    guard = PersonalityGuard()
    result = await guard.can_drift(guard_resident.id, db_session)
    assert result is False


@pytest.mark.anyio
async def test_can_drift_true_when_enough_memories_since_last_drift(db_session, guard_resident):
    """If 15+ event memories since last drift, allow."""
    # Set drift history 1 day ago
    history = PersonalityHistory(
        resident_id=guard_resident.id,
        trigger_type="drift",
        changes_json={"S1": {"from": "M", "to": "H"}},
        old_type="CTRL",
        new_type="CTRL",
        reason="Old drift",
        created_at=datetime.now(UTC) - timedelta(days=1),
    )
    db_session.add(history)
    await db_session.commit()

    # Add 15 event memories after the drift history
    for i in range(15):
        mem = Memory(
            resident_id=guard_resident.id,
            type="event",
            content=f"Post-drift event {i}",
            importance=0.5,
            source="chat_player",
        )
        db_session.add(mem)
    await db_session.commit()

    guard = PersonalityGuard()
    result = await guard.can_drift(guard_resident.id, db_session)
    assert result is True


# ── can_shift tests ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_can_shift_true_when_no_history(db_session, guard_resident):
    guard = PersonalityGuard()
    result = await guard.can_shift(guard_resident.id, db_session)
    assert result is True


@pytest.mark.anyio
async def test_can_shift_false_within_cooldown(db_session, guard_resident):
    """Shift within 24h cooldown → denied."""
    history = PersonalityHistory(
        resident_id=guard_resident.id,
        trigger_type="shift",
        changes_json={"E1": {"from": "H", "to": "L"}},
        old_type="CTRL",
        new_type="THIN-K",
        reason="Recent shift",
        created_at=datetime.now(UTC) - timedelta(hours=12),  # 12h ago, within 24h
    )
    db_session.add(history)
    await db_session.commit()

    guard = PersonalityGuard()
    result = await guard.can_shift(guard_resident.id, db_session)
    assert result is False


@pytest.mark.anyio
async def test_can_shift_true_after_cooldown(db_session, guard_resident):
    """Shift 25h ago → cooldown expired, allowed."""
    history = PersonalityHistory(
        resident_id=guard_resident.id,
        trigger_type="shift",
        changes_json={"E1": {"from": "H", "to": "L"}},
        old_type="CTRL",
        new_type="THIN-K",
        reason="Old shift",
        created_at=datetime.now(UTC) - timedelta(hours=25),
    )
    db_session.add(history)
    await db_session.commit()

    guard = PersonalityGuard()
    result = await guard.can_shift(guard_resident.id, db_session)
    assert result is True


# ── validate_drift tests ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_validate_drift_clamps_to_max_dimensions(db_session, guard_resident):
    """Drift with 4 dimensions → clamped to MAX_DRIFT_PER_CYCLE=2."""
    guard = PersonalityGuard()
    changes = {
        "S1": {"from": "M", "to": "H"},
        "E1": {"from": "H", "to": "M"},
        "A1": {"from": "M", "to": "H"},
        "So1": {"from": "L", "to": "M"},
    }
    clamped = await guard.validate_drift(changes, guard_resident.id, db_session)
    assert len(clamped) <= 2


@pytest.mark.anyio
async def test_validate_drift_rejects_L_to_H_jump(db_session, guard_resident):
    """Drift cannot jump L→H (step=1 only)."""
    guard = PersonalityGuard()
    changes = {
        "S1": {"from": "L", "to": "H"},  # invalid: 2-step jump
        "E2": {"from": "M", "to": "H"},  # valid: 1-step
    }
    clamped = await guard.validate_drift(changes, guard_resident.id, db_session)
    # L→H change should be removed; E2 change should remain
    assert "S1" not in clamped
    assert "E2" in clamped


@pytest.mark.anyio
async def test_validate_shift_clamps_to_max_dimensions(db_session, guard_resident):
    """Shift with 5 dimensions → clamped to MAX_SHIFT_PER_EVENT=3."""
    guard = PersonalityGuard()
    changes = {
        "S1": {"from": "H", "to": "L"},
        "E1": {"from": "H", "to": "L"},
        "A1": {"from": "M", "to": "H"},
        "So1": {"from": "M", "to": "L"},
        "Ac1": {"from": "H", "to": "M"},
    }
    clamped = await guard.validate_shift(changes, guard_resident.id, db_session)
    assert len(clamped) <= 3


@pytest.mark.anyio
async def test_validate_shift_allows_L_to_H(db_session, guard_resident):
    """Shift CAN jump L→H (step=2 allowed)."""
    guard = PersonalityGuard()
    changes = {"E1": {"from": "L", "to": "H"}}
    clamped = await guard.validate_shift(changes, guard_resident.id, db_session)
    assert "E1" in clamped
    assert clamped["E1"]["to"] == "H"


# ── check_monthly_budget tests ────────────────────────────────────────────


@pytest.mark.anyio
async def test_check_monthly_budget_full_at_start(db_session, guard_resident):
    guard = PersonalityGuard()
    remaining = await guard.check_monthly_budget(guard_resident.id, db_session)
    assert remaining == PersonalityGuard.TOTAL_MONTHLY_CHANGE


@pytest.mark.anyio
async def test_check_monthly_budget_decreases_with_changes(db_session, guard_resident):
    # Add a drift that changed 2 dimensions
    history = PersonalityHistory(
        resident_id=guard_resident.id,
        trigger_type="drift",
        changes_json={
            "S1": {"from": "M", "to": "H"},
            "E2": {"from": "H", "to": "M"},
        },
        old_type="CTRL",
        new_type="CTRL",
        reason="Test drift",
        created_at=datetime.now(UTC),
    )
    db_session.add(history)
    await db_session.commit()

    guard = PersonalityGuard()
    remaining = await guard.check_monthly_budget(guard_resident.id, db_session)
    # Each dimension change counts as 1 toward the budget
    assert remaining == PersonalityGuard.TOTAL_MONTHLY_CHANGE - 2


# ── staged pacing: world narrative time + real safety windows ─────────────


@pytest.mark.anyio
async def test_pacing_shift_cooldown_uses_world_hours(db_session, guard_resident, monkeypatch):
    from app import world_clock
    from app.config import settings
    real_now = datetime(2026, 8, 11, 8, tzinfo=UTC)
    monkeypatch.setattr(settings, "realism_personality_pacing_enabled", True)
    monkeypatch.setattr(settings, "world_clock_k", 4)
    monkeypatch.setattr(world_clock, "now_real", lambda: real_now)
    history = PersonalityHistory(
        resident_id=guard_resident.id, trigger_type="shift",
        changes_json={"E1": {"from": "H", "to": "M"}},
        old_type="CTRL", new_type="CTRL", reason="shift",
        created_at=real_now - timedelta(hours=5),  # 20 world hours
    )
    db_session.add(history)
    await db_session.commit()

    guard = PersonalityGuard()
    assert await guard.can_shift(guard_resident.id, db_session) is False
    history.created_at = real_now - timedelta(hours=7)  # 28 world hours
    await db_session.commit()
    assert await guard.can_shift(guard_resident.id, db_session) is True


@pytest.mark.anyio
async def test_pacing_drift_requires_72_world_hours_and_15_events(
    db_session, guard_resident, monkeypatch,
):
    from app import world_clock
    from app.config import settings
    real_now = datetime(2026, 8, 11, 8, tzinfo=UTC)
    monkeypatch.setattr(settings, "realism_personality_pacing_enabled", True)
    monkeypatch.setattr(settings, "world_clock_k", 4)
    monkeypatch.setattr(world_clock, "now_real", lambda: real_now)
    history = PersonalityHistory(
        resident_id=guard_resident.id, trigger_type="drift",
        changes_json={"S1": {"from": "M", "to": "H"}},
        old_type="CTRL", new_type="CTRL", reason="drift",
        created_at=real_now - timedelta(hours=17),  # 68 world hours
    )
    db_session.add(history)
    for i in range(15):
        db_session.add(Memory(
            resident_id=guard_resident.id, type="event", content=f"event {i}",
            importance=0.5, source="agent_action",
            created_at=real_now - timedelta(hours=1),
        ))
    await db_session.commit()

    guard = PersonalityGuard()
    assert await guard.can_drift(guard_resident.id, db_session) is False
    history.created_at = real_now - timedelta(hours=19)  # 76 world hours
    await db_session.commit()
    assert await guard.can_drift(guard_resident.id, db_session) is True


@pytest.mark.anyio
async def test_first_pacing_drift_uses_resident_creation_as_cooldown_anchor(
    db_session, guard_resident, monkeypatch,
):
    from app import world_clock
    from app.config import settings
    real_now = datetime(2026, 8, 11, 8, tzinfo=UTC)
    monkeypatch.setattr(settings, "realism_personality_pacing_enabled", True)
    monkeypatch.setattr(settings, "world_clock_k", 4)
    monkeypatch.setattr(world_clock, "now_real", lambda: real_now)
    guard_resident.created_at = real_now - timedelta(hours=17)
    for i in range(15):
        db_session.add(Memory(
            resident_id=guard_resident.id, type="event", content=f"event {i}",
            importance=0.5, source="agent_action",
        ))
    await db_session.commit()

    guard = PersonalityGuard()
    assert await guard.can_drift(guard_resident.id, db_session) is False
    guard_resident.created_at = real_now - timedelta(hours=19)
    await db_session.commit()
    assert await guard.can_drift(guard_resident.id, db_session) is True


@pytest.mark.anyio
async def test_pacing_budgets_are_real_rolling_windows(
    db_session, guard_resident, monkeypatch,
):
    from app import world_clock
    from app.config import settings
    real_now = datetime(2026, 8, 11, 8, tzinfo=UTC)
    monkeypatch.setattr(settings, "realism_personality_pacing_enabled", True)
    monkeypatch.setattr(world_clock, "now_real", lambda: real_now)
    db_session.add(PersonalityHistory(
        resident_id=guard_resident.id, trigger_type="drift",
        changes_json={
            "S1": {"from": "M", "to": "H"},
            "E1": {"from": "H", "to": "M"},
        },
        old_type="CTRL", new_type="CTRL", reason="recent drift",
        created_at=real_now - timedelta(days=6),
    ))
    await db_session.commit()

    guard = PersonalityGuard()
    # Ordinary drift: <=2 dimensions in any real rolling seven days.
    assert await guard.check_drift_budget(guard_resident.id, db_session) == 0
    # Total safety cap remains a real rolling 30-day window (not a world month).
    assert await guard.check_monthly_budget(guard_resident.id, db_session) == 6

    db_session.add(PersonalityHistory(
        resident_id=guard_resident.id, trigger_type="shift",
        changes_json={f"D{i}": {"from": "M", "to": "H"} for i in range(6)},
        old_type="CTRL", new_type="CTRL", reason="shift",
        created_at=real_now - timedelta(days=20),
    ))
    await db_session.commit()
    assert await guard.check_monthly_budget(guard_resident.id, db_session) == 0


@pytest.mark.anyio
async def test_pacing_clamps_ordinary_drift_to_one_dimension(
    db_session, guard_resident, monkeypatch,
):
    from app.config import settings
    monkeypatch.setattr(settings, "realism_personality_pacing_enabled", True)
    changes = {
        "S1": {"from": "M", "to": "H"},
        "E1": {"from": "H", "to": "M"},
    }
    validated = await PersonalityGuard().validate_drift(
        changes, guard_resident.id, db_session)
    assert list(validated) == ["S1"]
