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
  A vacate that actually landed then calls ``trigger_backfill`` — without it
  the office stays empty forever (F3 断链).

Term semantics: ``term_days`` are WORLD days. All conversion goes through
``app/world_clock.py`` (the single time-scale entry point) — never a bare
utcnow comparison against world rhythms. Stored datetimes are UTC-aware.

Mayor special-case: vacating the mayor office also clears the two legacy
stores — ``meta_json['mayor']`` (the wage-bonus multiplier, gotcha #1) and
``system_config['current_mayor']`` (the read-path fallback) — so the three
representations never diverge after a term expiry. Fail-open throughout.
Neither legacy-store query filters by resident_type / is_autonomous: the row
that must be cleaned is precisely the one that may have just left that set.

S10 —— 与 ``meta_json['duty']`` 的边界(``app/services/duty_service.py`` 顶部有
对向的同一段说明):

官职(下面的 ``OFFICE_DEFS``,4 键)与营生(``duty_service``,11 键)是**两个概
念**,不是同一概念的两套存储。重叠只有 ``{town_clerk, postman}``。迁移 046
曾做一次快照，但 roster reset 会先腾空 office 再重建居民；
:func:`reconcile_seed_offices` 在 reset 末尾幂等补回这两个 seed 席位，并拒绝
覆盖任何非空冲突。其余 duty 与 mayor/doctor 始终不互抄。

镇长是官职,从来不是营生;``mayor`` 的权威读法是
``election_service.current_mayor``,不是裸读本表(见 ``_effective_holder``)。
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

# Only these labour duties intentionally overlap the offices table.  Mayor and
# doctor have no legacy duty counterpart; private duties must never be copied
# into offices merely because both concepts have a human-readable title.
SEED_DUTY_OFFICE_KEYS: tuple[str, ...] = ("town_clerk", "postman")


async def reconcile_seed_offices(
    db: AsyncSession, *, apply: bool = False,
) -> dict:
    """Plan or apply the two safe seed-duty → office appointments.

    Vacancies are filled only when exactly one autonomous resident carries the
    matching duty. A non-vacant disagreement is reported as a conflict and is
    never overwritten. The default is a read-only dry run so operators can use
    this function directly against production before choosing ``apply=True``.
    """
    from app.models.resident import Resident
    from app.services.duty_service import duty_key

    residents = (await db.execute(
        select(Resident).where(
            Resident.is_autonomous,
            Resident.meta_json.isnot(None),
        ).order_by(Resident.slug)
    )).scalars().all()
    candidates = {
        key: [r.slug for r in residents if duty_key(r) == key]
        for key in SEED_DUTY_OFFICE_KEYS
    }
    holders = dict((await db.execute(
        select(Office.office_key, Office.holder_slug).where(
            Office.office_key.in_(SEED_DUTY_OFFICE_KEYS)
        )
    )).all())

    report: dict = {
        "dry_run": not apply,
        "would_appoint": [],
        "appointed": [],
        "unchanged": [],
        "missing": [],
        "ambiguous": {},
        "conflicts": {},
    }
    service = OfficeService(db)
    for key in SEED_DUTY_OFFICE_KEYS:
        slugs = candidates[key]
        if not slugs:
            report["missing"].append(key)
            continue
        if len(slugs) != 1:
            report["ambiguous"][key] = slugs
            continue
        expected = slugs[0]
        actual = holders.get(key)
        if actual == expected:
            report["unchanged"].append({"office_key": key, "holder": expected})
            continue
        if actual:
            report["conflicts"][key] = {
                "office_holder": actual,
                "duty_holder": expected,
            }
            continue
        item = {"office_key": key, "holder": expected}
        if not apply:
            report["would_appoint"].append(item)
            continue
        if await service.appoint_if_vacant(
            key, expected, fill_strategy="seed",
        ):
            report["appointed"].append(item)
        else:
            raced_holder = await service.get_holder(key)
            report["conflicts"][key] = {
                "office_holder": raced_holder,
                "duty_holder": expected,
                "reason": "concurrent_appointment_or_failure",
            }
    return report


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

    async def appoint_if_vacant(
        self, office_key: str, slug: str, *, fill_strategy: str,
        term_days: int | None = None,
    ) -> bool:
        """Install a holder only while the seat is vacant.

        Unlike :meth:`appoint`, this never transfers a non-empty office. It is
        the reconciliation primitive: a concurrent appointment between dry
        read and apply is observed as ``False``, never overwritten.
        """
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
        for _attempt in range(3):
            result = await self.db.execute(
                update(Office)
                .where(
                    Office.office_key == office_key,
                    Office.holder_slug.is_(None),
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if (result.rowcount or 0) > 0:
                await self.db.commit()
                await self._emit_office_changed(
                    "office_appointed", office_key, holder_slug=slug,
                )
                return True

            existing = (await self.db.execute(
                select(Office.holder_slug).where(
                    Office.office_key == office_key
                )
            )).scalar_one_or_none()
            if existing is not None:
                await self.db.commit()  # end the read transaction / any lock
                return existing == slug

            defaults = OFFICE_DEFS.get(office_key, {})
            self.db.add(Office(
                office_key=office_key,
                institution=defaults.get("institution", _DEFAULT_INSTITUTION),
                perms_json={},
                **values,
            ))
            try:
                await self.db.commit()
                await self._emit_office_changed(
                    "office_appointed", office_key, holder_slug=slug,
                )
                return True
            except IntegrityError:
                await self.db.rollback()
                continue
        logger.warning(
            "vacant-only office appoint lost upsert race: %s", office_key,
        )
        return False

    async def vacate(self, office_key: str, *, audit: bool = False) -> bool:
        """Clear the office holder + term end. Guard UPDATE — returns True
        only when an actual holder was cleared (idempotent no-op otherwise).

        ``audit=True`` additionally files a read-only term audit for the
        departing holder (F2's revocation path uses it; default False keeps
        every existing caller byte-identical). F2's ``revoke_citizenship``
        does NOT call this method at all — it hand-rolls its own
        ``update(Office)`` because ``vacate`` commits internally, which does
        not compose with F2's own transaction. ``audit=True`` is for OTHER
        callers only (term_check uses its own inline copy of this same
        sequence, not this method).

        The pre-read is NOT a guard (the UPDATE's rowcount still decides): it
        captures who is leaving and when the term began, because both the
        legacy-store cleanup and the audit are keyed on that identity.
        """
        prior_row = (await self.db.execute(
            select(Office).where(Office.office_key == office_key)
        )).scalar_one_or_none()
        prior_holder = prior_row.holder_slug if prior_row is not None else None
        prior_started = prior_row.term_started_at if prior_row is not None else None
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
            if audit:
                from app.tasks import office_audit
                await office_audit.record_term_audit(
                    self.db, office_key=office_key, holder_slug=prior_holder,
                    term_started_at=prior_started,
                )
        return vacated

    async def term_check(self, *, now: datetime | None = None) -> int:
        """Nightly: vacate every office whose term_ends_at has passed, then
        hand each freed seat to :func:`trigger_backfill`.

        Returns the number of offices actually vacated. ``now`` is injectable
        for frozen-clock tests; the default reads the world clock's real 'now'
        (term_ends_at is stored in real UTC, converted at appoint time).

        The backfill call is the F3 断链 fix: before it, an expired term left
        the office empty AND cleared both current_mayor fallbacks, so nothing
        in the world could ever seat a successor.
        """
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
        # Extracted into plain tuples immediately — the loop below must never
        # touch these ORM objects again. trigger_backfill's fail-open path
        # (_rollback_quietly) calls db.rollback(), which — unlike commit()
        # under expire_on_commit=False — unconditionally expires the WHOLE
        # identity map, including whichever `due` rows this loop hasn't
        # reached yet. A later `office.office_key` read on an expired
        # AsyncSession-loaded instance triggers an implicit lazy-refresh
        # outside of any greenlet_spawn context and raises MissingGreenlet
        # (fix round 1: reproduced with 2 due offices where the first's
        # backfill fails — the second's attribute read crashed term_check
        # outright, taking down the whole nightly cron office segment).
        due_rows = [(o.id, o.office_key, o.holder_slug, o.term_started_at) for o in due]
        n = 0
        for office_id, office_key, prior_holder, prior_started in due_rows:
            res = await self.db.execute(
                update(Office)
                .where(
                    Office.id == office_id,
                    Office.holder_slug.isnot(None),
                    Office.term_ends_at <= now,
                )
                .values(holder_slug=None, term_ends_at=None,
                        updated_at=datetime.now(UTC))
                .execution_options(synchronize_session=False)
            )
            if (res.rowcount or 0) == 0:
                continue  # re-appointed concurrently — not expired anymore
            if office_key == "mayor":
                await self._clear_mayor_legacy_stores(holder_slug=prior_holder)
            await self.db.commit()
            n += 1
            await self._emit_office_changed(
                "office_vacated", office_key, holder_slug=None,
            )
            # F3: 卸任财政审计 — read-only, fail-open, and chronologically
            # before the backfill (it summarises the term that just ended).
            # It runs AFTER the vacate's own commit() above, so a failure
            # here (record_term_audit's own _rollback_quietly) only ever
            # rolls back its OWN not-yet-committed work — the vacate and the
            # legacy-store cleanup are already durable and cannot be undone
            # by it. due_rows was extracted into plain tuples before this
            # loop started specifically so that a rollback-triggered identity
            # map expiry here can never strand a later iteration's ORM read
            # (see the due_rows comment above) — office_key/prior_holder/
            # prior_started are plain values, and trigger_backfill below only
            # issues fresh SELECTs, never touching a previously-loaded row.
            from app.tasks import office_audit
            await office_audit.record_term_audit(
                self.db, office_key=office_key, holder_slug=prior_holder,
                term_started_at=prior_started, term_ended_at=now,
            )
            # F3: the second half. trigger_backfill is fail-open internally,
            # so a broken election can never turn a completed vacate into an
            # exception. It runs AFTER the legacy stores were cleared above —
            # that ordering is what makes the vacancy visible to it.
            await trigger_backfill(
                self.db, office_key, reason=REASON_TERM_EXPIRED,
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
    """The office's refill procedure.

    Falls back to OFFICE_DEFS when the row is missing: with
    ``polis_office_enabled`` off the migration-046 seed may never have run in
    this world, and "no row" must not be read as "not an elected office".
    """
    try:
        row = (await db.execute(
            select(Office.fill_strategy).where(Office.office_key == office_key)
        )).scalar_one_or_none()
    except Exception:
        logger.warning("offices fill_strategy lookup failed: %s", office_key,
                       exc_info=True)
        row = None
    if row:
        return str(row)
    return str(OFFICE_DEFS.get(office_key, {}).get("fill_strategy") or "")


async def _effective_holder(db: AsyncSession, office_key: str) -> str | None:
    """Who effectively holds ``office_key`` right now, under EITHER gate state.

    For the mayor this must NOT be a raw ``offices`` read: correctness may not
    depend on ``polis_office_enabled``. With the gate off the offices row can
    be absent entirely, or carry a stale migration-046 holder_slug that no
    business path honours. ``election_service.current_mayor`` is the one read
    that already encodes both worlds (offices when the gate is on, then the
    ``system_config['current_mayor']`` fallback).
    """
    if office_key == "mayor":
        from app.services import election_service
        return await election_service.current_mayor(db)
    return await OfficeService(db).get_holder(office_key)


async def trigger_backfill(
    db: AsyncSession, office_key: str, *, reason: str,
) -> str | None:
    """Refill a now-vacant office. Returns the opened Poll.id, else None.

    None means: not an elected office / still occupied / an election poll is
    already open / election|civic gates off / not enough candidates / an
    internal failure (fail-open — a broken election must never break the
    vacate that called us).

    CALL ORDER (F2 contract): call this only after both legacy mayor stores
    (``meta_json['mayor']`` and ``system_config['current_mayor']``) have been
    cleared. ``_effective_holder`` reads the fallback on purpose, so calling
    too early reports "still occupied" and silently skips the backfill.
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
