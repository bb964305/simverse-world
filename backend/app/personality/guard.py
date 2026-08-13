import logging
from datetime import datetime, UTC, timedelta
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.personality_history import PersonalityHistory
from app.models.memory import Memory

logger = logging.getLogger(__name__)

# L/M/H ordering for step validation
_LEVEL_ORDER = {"L": 0, "M": 1, "H": 2}


def _step_distance(frm: str, to: str) -> int:
    """Absolute step distance between two L/M/H levels."""
    return abs(_LEVEL_ORDER.get(to, 1) - _LEVEL_ORDER.get(frm, 1))


class PersonalityGuard:
    """Enforces rate limits and validity constraints on personality evolution.

    All validate_* methods return a (possibly clamped/filtered) subset of
    the proposed changes dict. They never raise — callers get fewer changes,
    never an exception.
    """

    MAX_DRIFT_PER_CYCLE: int = 2
    MAX_SHIFT_PER_EVENT: int = 3
    DRIFT_STEP: int = 1        # max single-step distance for drift
    SHIFT_STEP: int = 2        # max single-step distance for shift (L→H allowed)
    MIN_DRIFT_INTERVAL: int = 15  # event memories required since last drift
    SHIFT_COOLDOWN_HOURS: int = 24
    TOTAL_MONTHLY_CHANGE: int = 8  # legacy calendar month / paced rolling real 30d
    DRIFT_ROLLING_7D_CHANGE: int = 2

    async def can_drift(self, resident_id: str, db: AsyncSession) -> bool:
        """Return True if enough event memories have accumulated since last drift.

        If there has never been a drift, allow drift immediately (no memory threshold).
        If there was a previous drift, require MIN_DRIFT_INTERVAL event memories since then.
        """
        # Find timestamp of most recent drift
        stmt = (
            select(PersonalityHistory.created_at)
            .where(
                PersonalityHistory.resident_id == resident_id,
                PersonalityHistory.trigger_type == "drift",
            )
            .order_by(PersonalityHistory.created_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        last_drift_at = result.scalar_one_or_none()

        # Legacy residents drifted immediately once.  V2 requires an evidence
        # floor even for the first drift, preventing a new personality from
        # changing after its first chat wrap-up.
        if last_drift_at is None:
            if not _pacing_enabled():
                return True
            # With no previous drift, the resident's creation is the cooldown
            # anchor. Otherwise a busy new resident could mutate after merely
            # fifteen tick memories, long before the 72-world-hour narrative
            # interval has elapsed.
            from app.models.resident import Resident
            created_at = (await db.execute(
                select(Resident.created_at).where(Resident.id == resident_id)
            )).scalar_one_or_none()
            if created_at is None:
                return False
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            from app import world_clock
            from app.config import settings
            elapsed_world = world_clock.now_world() - world_clock.real_to_world(created_at)
            if elapsed_world < timedelta(hours=settings.realism_drift_min_world_hours):
                return False
            count_stmt = select(func.count()).select_from(Memory).where(
                Memory.resident_id == resident_id,
                Memory.type == "event",
            )
            count = (await db.execute(count_stmt)).scalar_one()
            return count >= self.MIN_DRIFT_INTERVAL

        # Make timezone-naive datetimes from SQLite timezone-aware
        if last_drift_at.tzinfo is None:
            last_drift_at = last_drift_at.replace(tzinfo=UTC)

        if _pacing_enabled():
            from app import world_clock
            elapsed_world = world_clock.now_world() - world_clock.real_to_world(last_drift_at)
            from app.config import settings
            if elapsed_world < timedelta(hours=settings.realism_drift_min_world_hours):
                return False

        # Count event memories since last drift
        count_stmt = select(func.count()).select_from(Memory).where(
            Memory.resident_id == resident_id,
            Memory.type == "event",
            Memory.created_at > last_drift_at,
        )

        result = await db.execute(count_stmt)
        count = result.scalar_one()
        return count >= self.MIN_DRIFT_INTERVAL

    async def can_shift(self, resident_id: str, db: AsyncSession) -> bool:
        """Return True if 24h cooldown has elapsed since last shift."""
        stmt = (
            select(PersonalityHistory.created_at)
            .where(
                PersonalityHistory.resident_id == resident_id,
                PersonalityHistory.trigger_type == "shift",
            )
            .order_by(PersonalityHistory.created_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        last_shift_at = result.scalar_one_or_none()

        if last_shift_at is None:
            return True

        # Make timezone-naive datetimes from SQLite timezone-aware
        if last_shift_at.tzinfo is None:
            last_shift_at = last_shift_at.replace(tzinfo=UTC)

        if _pacing_enabled():
            from app import world_clock
            elapsed = world_clock.now_world() - world_clock.real_to_world(last_shift_at)
        else:
            elapsed = datetime.now(UTC) - last_shift_at
        return elapsed >= timedelta(hours=self.SHIFT_COOLDOWN_HOURS)

    async def validate_drift(
        self,
        changes: dict[str, dict],
        resident_id: str,
        db: AsyncSession,
    ) -> dict[str, dict]:
        """Validate and clamp drift changes.

        Rules:
        - Remove any change where step distance > DRIFT_STEP (no L→H)
        - Keep at most MAX_DRIFT_PER_CYCLE dimensions
        """
        valid = {
            dim: change
            for dim, change in changes.items()
            if _step_distance(change["from"], change["to"]) <= self.DRIFT_STEP
        }
        max_changes = 1 if _pacing_enabled() else self.MAX_DRIFT_PER_CYCLE
        if len(valid) > max_changes:
            # Keep the first N (LLM ordering reflects priority)
            keys = list(valid.keys())[:max_changes]
            valid = {k: valid[k] for k in keys}
        return valid

    async def validate_shift(
        self,
        changes: dict[str, dict],
        resident_id: str,
        db: AsyncSession,
    ) -> dict[str, dict]:
        """Validate and clamp shift changes.

        Rules:
        - All step distances allowed (L→H OK for shift)
        - Keep at most MAX_SHIFT_PER_EVENT dimensions
        """
        if len(changes) > self.MAX_SHIFT_PER_EVENT:
            keys = list(changes.keys())[: self.MAX_SHIFT_PER_EVENT]
            return {k: changes[k] for k in keys}
        return dict(changes)

    async def check_monthly_budget(
        self, resident_id: str, db: AsyncSession
    ) -> int:
        """Return remaining total dimension-change budget.

        Legacy mode counts the current calendar month. Paced mode uses a real
        rolling 30-day safety window so an accelerated world clock cannot
        multiply the allowed mutation rate.
        """
        if _pacing_enabled():
            # This is a safety budget, not resident-calendar narrative.  Keep it
            # on real rolling time so k=4 cannot multiply the allowed mutation
            # rate fourfold: at most 8 dimensions in any real 30-day window.
            from app import world_clock
            since = world_clock.now_real().astimezone(UTC) - timedelta(days=30)
            stmt = select(PersonalityHistory.changes_json).where(
                PersonalityHistory.resident_id == resident_id,
                PersonalityHistory.created_at >= since,
            )
        else:
            from sqlalchemy import extract
            now = datetime.now(UTC)
            stmt = (
                select(PersonalityHistory.changes_json)
                .where(
                    PersonalityHistory.resident_id == resident_id,
                    extract("year", PersonalityHistory.created_at) == now.year,
                    extract("month", PersonalityHistory.created_at) == now.month,
                )
            )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        used = sum(len(row) for row in rows if isinstance(row, dict))
        return max(0, self.TOTAL_MONTHLY_CHANGE - used)

    async def check_drift_budget(self, resident_id: str, db: AsyncSession) -> int:
        """Remaining ordinary-drift budget.

        V2 combines the total rolling-30d cap with a rolling-7d ordinary cap;
        dramatic shifts use only the total cap.  Legacy mode preserves the old
        calendar-month calculation.
        """
        total_remaining = await self.check_monthly_budget(resident_id, db)
        if not _pacing_enabled():
            return total_remaining
        from app import world_clock
        since = world_clock.now_real().astimezone(UTC) - timedelta(days=7)
        stmt = select(PersonalityHistory.changes_json).where(
            PersonalityHistory.resident_id == resident_id,
            PersonalityHistory.trigger_type == "drift",
            PersonalityHistory.created_at >= since,
        )
        rows = (await db.execute(stmt)).scalars().all()
        used = sum(len(row) for row in rows if isinstance(row, dict))
        return min(total_remaining, max(0, self.DRIFT_ROLLING_7D_CHANGE - used))


def _pacing_enabled() -> bool:
    from app.config import settings
    return bool(settings.realism_personality_pacing_enabled)
