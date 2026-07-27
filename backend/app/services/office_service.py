"""S2-1 OfficeService — the unified appoint/vacate/term_check entry point.

Pure rules, zero LLM. Write paths are conditional UPDATE + upsert (the
coin_service atomicity pattern — never SELECT-then-write):

- ``appoint``: ``UPDATE ... WHERE office_key = :key``; rowcount==0 → INSERT,
  and on the UNIQUE(office_key) race an IntegrityError falls through to a
  retry UPDATE (mirrors ``coin_service.treasury_credit_pending``'s generic
  branch + charge()'s guard-UPDATE discipline).
- ``vacate``: guard UPDATE ``WHERE office_key = :key AND holder_slug IS NOT
  NULL`` — rowcount decides whether a real vacate happened.
- ``term_check``: per-row guard UPDATE ``WHERE id = :id AND term_ends_at <=
  :now`` so a concurrent re-appoint between SELECT and UPDATE is never
  clobbered.

Term semantics: ``term_days`` are WORLD days. All conversion goes through
``app/world_clock.py`` (the single time-scale entry point) — never a bare
utcnow comparison against world rhythms. Stored datetimes are UTC-aware.

Mayor special-case: vacating the mayor office also clears the two legacy
stores — ``meta_json['mayor']`` (the wage-bonus multiplier, gotcha #1) and
``system_config['current_mayor']`` (the read-path fallback) — so the three
representations never diverge after a term expiry. Fail-open throughout.
Neither legacy-store query filters by resident_type / is_autonomous: the row
that must be cleaned is precisely the one that may have just left that set.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, UTC

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.office import Office

logger = logging.getLogger(__name__)

# Default definitions for the four S2-1 offices — used when an appoint has to
# INSERT a row that the migration seed did not create (fresh world, tests).
OFFICE_DEFS: dict[str, dict] = {
    "mayor": {"institution": "town_hall", "fill_strategy": "election"},
    "town_clerk": {"institution": "town_hall", "fill_strategy": "seed"},
    "postman": {"institution": "post_office", "fill_strategy": "seed"},
    "doctor": {"institution": "clinic", "fill_strategy": "appointment"},
}
_DEFAULT_INSTITUTION = "town_hall"


def _term_window(term_days: int | None) -> tuple[datetime, datetime | None]:
    """(term_started_at, term_ends_at) in UTC. ``term_days`` are WORLD days;
    None/0/negative = unlimited term (NULL end). Conversion via world_clock."""
    from app import world_clock

    now_r = world_clock.now_real()
    started = now_r.astimezone(UTC)
    if not term_days or term_days <= 0:
        return started, None
    ends_world = world_clock.real_to_world(now_r) + timedelta(days=term_days)
    return started, world_clock.world_to_real(ends_world).astimezone(UTC)


class OfficeService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── write paths (atomic) ───────────────────────────────────────────

    async def appoint(
        self, office_key: str, slug: str, *,
        fill_strategy: str, term_days: int | None = None,
    ) -> bool:
        """Install ``slug`` as the office holder (upsert, atomic). Returns
        True on success. An existing holder is overwritten — transfer is a
        single conditional UPDATE, matching install_mayor's overwrite
        semantics."""
        if not office_key or not slug:
            return False
        started, ends = _term_window(term_days)
        values = {
            "holder_slug": slug,
            "fill_strategy": fill_strategy,
            "term_started_at": started,
            "term_ends_at": ends,
            "updated_at": datetime.now(UTC),
        }
        landed = False
        for _attempt in range(3):
            res = await self.db.execute(
                update(Office)
                .where(Office.office_key == office_key)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if (res.rowcount or 0) > 0:
                await self.db.commit()
                landed = True
                break
            defaults = OFFICE_DEFS.get(office_key, {})
            self.db.add(Office(
                office_key=office_key,
                institution=defaults.get("institution", _DEFAULT_INSTITUTION),
                perms_json={},
                **values,
            ))
            try:
                await self.db.commit()
                landed = True
                break
            except IntegrityError:
                # lost the insert race (UNIQUE office_key) → loop back to the
                # conditional UPDATE against the winner's row. The retry
                # UPDATE re-checks rowcount — a 0-row retry must never be
                # reported as success (lost-update door).
                await self.db.rollback()
                continue
        if not landed:
            logger.warning("office appoint lost upsert race repeatedly: %s", office_key)
            return False
        await self._emit_office_changed(
            "office_appointed", office_key, holder_slug=slug,
        )
        return True

    async def vacate(self, office_key: str) -> bool:
        """Clear the office holder + term end. Guard UPDATE — returns True
        only when an actual holder was cleared (idempotent no-op otherwise).

        The pre-read is NOT a guard (the UPDATE's rowcount still decides): it
        only captures who is leaving, because the legacy-store cleanup must be
        keyed on that identity rather than on a membership predicate."""
        prior_row = (await self.db.execute(
            select(Office).where(Office.office_key == office_key)
        )).scalar_one_or_none()
        prior_holder = prior_row.holder_slug if prior_row is not None else None
        res = await self.db.execute(
            update(Office)
            .where(Office.office_key == office_key, Office.holder_slug.isnot(None))
            .values(holder_slug=None, term_ends_at=None,
                    updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        vacated = (res.rowcount or 0) > 0
        if vacated and office_key == "mayor":
            await self._clear_mayor_legacy_stores(holder_slug=prior_holder)
        await self.db.commit()
        if vacated:
            await self._emit_office_changed(
                "office_vacated", office_key, holder_slug=None,
            )
        return vacated

    async def term_check(self, *, now: datetime | None = None) -> int:
        """Nightly: vacate every office whose term_ends_at has passed.
        Returns the number of offices vacated. ``now`` is injectable for
        frozen-clock tests; the default reads the world clock's real 'now'
        (term_ends_at is stored in real UTC, converted at appoint time)."""
        if now is None:
            from app import world_clock
            now = world_clock.now_real().astimezone(UTC)
        due = (await self.db.execute(
            select(Office).where(
                Office.holder_slug.isnot(None),
                Office.term_ends_at.isnot(None),
                Office.term_ends_at <= now,
            )
        )).scalars().all()
        n = 0
        for office in due:
            res = await self.db.execute(
                update(Office)
                .where(
                    Office.id == office.id,
                    Office.holder_slug.isnot(None),
                    Office.term_ends_at <= now,
                )
                .values(holder_slug=None, term_ends_at=None,
                        updated_at=datetime.now(UTC))
                .execution_options(synchronize_session=False)
            )
            if (res.rowcount or 0) == 0:
                continue  # re-appointed concurrently — not expired anymore
            if office.office_key == "mayor":
                await self._clear_mayor_legacy_stores(
                    holder_slug=office.holder_slug,
                )
            await self.db.commit()
            n += 1
            await self._emit_office_changed(
                "office_vacated", office.office_key, holder_slug=None,
            )
        return n

    # ── read paths ─────────────────────────────────────────────────────

    async def get_holder(self, office_key: str) -> str | None:
        return (await self.db.execute(
            select(Office.holder_slug).where(Office.office_key == office_key)
        )).scalar_one_or_none()

    async def list_offices(self) -> list[dict]:
        rows = (await self.db.execute(
            select(Office).order_by(Office.id)
        )).scalars().all()
        return [
            {
                "office_key": o.office_key,
                "holder_slug": o.holder_slug,
                "institution": o.institution,
                "perms_json": o.perms_json or {},
                "fill_strategy": o.fill_strategy,
                "term_started_at": o.term_started_at.isoformat() if o.term_started_at else None,
                "term_ends_at": o.term_ends_at.isoformat() if o.term_ends_at else None,
                "updated_at": o.updated_at.isoformat() if o.updated_at else None,
            }
            for o in rows
        ]

    # ── internals ──────────────────────────────────────────────────────

    async def _clear_mayor_legacy_stores(
        self, *, holder_slug: str | None = None,
    ) -> None:
        """Keep the two legacy mayor stores in step with an offices-side
        vacate: pop meta_json['mayor'] (the wage multiplier — gotcha #1) and
        null system_config['current_mayor'] (the read fallback). Flushed into
        the caller's transaction; fail-open.

        NEITHER query may carry a membership predicate. The pre-F3 version
        scanned ``WHERE Resident.is_autonomous``, i.e. it selected the set the
        departing holder may have just left (demoted / exiled / a player
        avatar) — exactly the row that must be cleaned. Two disjoint reads
        replace it:

        1. targeted — ``WHERE slug = :holder_slug``, identity not membership;
        2. residual — ``WHERE meta_json IS NOT NULL AND slug <> :holder_slug``,
           catching stale flags any other path left behind.
        """
        try:
            from sqlalchemy.orm.attributes import flag_modified
            from app.models.resident import Resident

            targets: list = []
            if holder_slug:
                leaving = (await self.db.execute(
                    select(Resident).where(Resident.slug == holder_slug)
                )).scalar_one_or_none()
                if leaving is not None:
                    targets.append(leaving)
            residual_stmt = select(Resident).where(Resident.meta_json.isnot(None))
            if holder_slug:
                residual_stmt = residual_stmt.where(Resident.slug != holder_slug)
            targets.extend((await self.db.execute(residual_stmt)).scalars().all())

            for r in targets:
                meta = dict(r.meta_json or {})
                if meta.get("mayor"):
                    meta.pop("mayor", None)
                    r.meta_json = meta
                    flag_modified(r, "meta_json")
            from app.models.system_config import SystemConfig
            import json
            cfg = (await self.db.execute(
                select(SystemConfig).where(SystemConfig.key == "current_mayor")
            )).scalar_one_or_none()
            if cfg is not None:
                cfg.value = json.dumps(None)
                cfg.updated_by = "office_term_check"
                cfg.updated_at = datetime.now(UTC)
        except Exception:
            logger.warning("clearing legacy mayor stores failed", exc_info=True)

    async def _emit_office_changed(
        self, action: str, office_key: str, *, holder_slug: str | None,
    ) -> None:
        """Broadcast an ``office_changed`` WS event anchored to the current
        world revision/seq (world_changed v1 shape; seq reuses the OutboxEvent
        cursor — no new counter). Gated + fail-open: never breaks a write."""
        try:
            from app.config import settings
            if not settings.polis_office_enabled:
                return
            from app.services import world_revision_service as wrsvc
            seq = await wrsvc.current_source_cursor(self.db)
            revision_id = await wrsvc.current_revision_id(self.db)
            payload = {
                "type": "office_changed",
                "schema_version": wrsvc.SCHEMA_VERSION,
                "event_id": str(uuid.uuid4()),
                "seq": seq,
                "world_revision_id": revision_id,
                "action": action,
                "office_key": office_key,
                "holder_slug": holder_slug,
                "occurred_at": datetime.now(UTC).isoformat(),
            }
            from app.lab.apply import broadcast_world_changed
            await broadcast_world_changed(payload)
        except Exception:
            logger.warning("office_changed broadcast failed", exc_info=True)


# ── F3: the missing second half of a vacancy ───────────────────────────
#
# term_check() only ever vacated. current_mayor()'s two fallbacks were cleared
# by the same pass, so the world settled into "no mayor and nobody arriving".
# trigger_backfill is that missing link, and it is also the single entry point
# F2's revocation calls (KICKOFF 2026-07-27 §5 与 F2 的接口约定).

REASON_TERM_EXPIRED = "term_expired"
REASON_CIVIC_REVOCATION = "civic_revocation"
REASON_MANUAL = "manual"


async def _rollback_quietly(db: AsyncSession) -> None:
    """Roll back after a swallowed failure so the caller's session stays
    usable. Never raises — a fail-open path may not explode on its way out."""
    try:
        await db.rollback()
    except Exception:
        logger.warning("rollback after a swallowed office failure also failed",
                       exc_info=True)


async def _fill_strategy(db: AsyncSession, office_key: str) -> str:
    """The office's refill procedure, read off the offices row."""
    row = (await db.execute(
        select(Office.fill_strategy).where(Office.office_key == office_key)
    )).scalar_one_or_none()
    return str(row or "")


async def _effective_holder(db: AsyncSession, office_key: str) -> str | None:
    """Who holds ``office_key`` right now."""
    return await OfficeService(db).get_holder(office_key)


async def trigger_backfill(
    db: AsyncSession, office_key: str, *, reason: str,
) -> str | None:
    """Refill a now-vacant office. Returns the opened Poll.id, else None.

    None means: not an elected office / still occupied / an election poll is
    already open / election|civic gates off / not enough candidates / an
    internal failure (fail-open — a broken election must never break the
    vacate that called us).
    """
    try:
        if not office_key:
            return None
        if await _fill_strategy(db, office_key) != "election":
            return None
        from app.config import settings
        if not (settings.election_enabled and settings.civic_polls_enabled):
            return None
        if await _effective_holder(db, office_key):
            return None

        from app.models.season import Poll
        from app.services import election_service
        existing = (await db.execute(
            select(Poll).where(
                Poll.status == "open",
                Poll.question.like(f"{election_service.ELECTION_TAG}%"),
            )
        )).scalars().first()
        if existing is not None:
            logger.info("office backfill skipped (%s/%s): election already open",
                        office_key, reason)
            return None

        poll = await election_service.open_election(db)
        if poll is None:
            logger.info("office backfill produced no poll (%s/%s)",
                        office_key, reason)
            return None
        try:
            from app.services.config_service import ConfigService
            await ConfigService(db).set(
                "election_last_opened",
                datetime.now(UTC).date().isoformat(),
                group="civic", updated_by=f"office_backfill:{reason}",
            )
        except Exception:
            logger.warning("stamping election_last_opened failed", exc_info=True)
            # Same reason as the outer handler: ConfigService.set writes and
            # commits, so a failure here can leave the session needing a
            # rollback. The poll itself is already committed by propose().
            await _rollback_quietly(db)
        logger.info("office backfill opened election %s for %s (%s)",
                    poll.id, office_key, reason)
        return poll.id
    except Exception:
        logger.warning("office backfill failed (%s/%s)", office_key, reason,
                       exc_info=True)
        # Fail-open has to cover the SESSION, not just the return value.
        # open_election → civic_service.propose does db.add + db.commit, so the
        # exception may well come out of a flush/commit (IntegrityError, a
        # dropped connection, a column-width overflow). A session left in the
        # needs-rollback state makes every LATER statement raise
        # PendingRollbackError — i.e. returning None would merely move the
        # explosion one statement down (term_check's next due office, or F2's
        # 改档位 → 写历史行 → 广播 after vacate returned True).
        await _rollback_quietly(db)
        return None
