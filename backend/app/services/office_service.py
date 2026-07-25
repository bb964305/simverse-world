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
        only when an actual holder was cleared (idempotent no-op otherwise)."""
        res = await self.db.execute(
            update(Office)
            .where(Office.office_key == office_key, Office.holder_slug.isnot(None))
            .values(holder_slug=None, term_ends_at=None,
                    updated_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        vacated = (res.rowcount or 0) > 0
        if vacated and office_key == "mayor":
            await self._clear_mayor_legacy_stores()
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
                await self._clear_mayor_legacy_stores()
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

    async def _clear_mayor_legacy_stores(self) -> None:
        """Keep the two legacy mayor stores in step with an offices-side
        vacate: pop meta_json['mayor'] everywhere (the wage multiplier —
        gotcha #1) and null system_config['current_mayor'] (the read
        fallback). Flushed into the caller's transaction; fail-open."""
        try:
            from sqlalchemy.orm.attributes import flag_modified
            from app.models.resident import Resident

            residents = (await self.db.execute(
                select(Resident).where(
                    Resident.resident_type == "npc",
                    Resident.meta_json.isnot(None),
                )
            )).scalars().all()
            for r in residents:
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
