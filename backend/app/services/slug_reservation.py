"""Durable, cross-worker resident-slug reservations.

The existing ``forge_sessions.target_slug`` unique index is the serialization
point for every Forge and import path. Canonical/legacy Forge sessions own their
reservation directly; imports use a lightweight ``slug_reservation`` session
row and consume it in the same transaction as the Resident insert.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.forge.runtime_limits import FORGE_GENERATION_STALE_AFTER
from app.config import settings
from app.models.forge_session import ForgeSession
from app.models.resident import Resident


RESERVATION_MODE = "slug_reservation"
RESERVATION_STATUS = "reserved"
SLUG_MAX_CHARS = 100
_MAX_SUFFIX_ATTEMPTS = 8

# A multipart import can make two serial LLM calls (format conversion + SBTI).
# Anthropic's timeout applies to every attempt and the SDK accepts Retry-After
# delays up to 60 seconds, so one import's configured upper bound is:
#
#   2 * ((retries + 1) * timeout + retries * 60s)
#
# A single explicit deadline covers quota-lock waiting, both paid calls and the
# final atomic commit.  The reservation lease is always longer than that
# deadline, so queue depth and UTC quota resets cannot invalidate the proof.
# Unlike a Forge runner, an import has no other outer pipeline timeout.
_IMPORT_LLM_CALLS = 2
_SDK_MAX_RETRY_AFTER_SECONDS = 60
_IMPORT_WORK_HEADROOM_SECONDS = 10 * 60
_IMPORT_WORK_TIMEOUT_FLOOR_SECONDS = 30 * 60
_IMPORT_RELEASE_GRACE_SECONDS = 10 * 60


class SlugReservationConflict(ValueError):
    """The requested resident slug is already occupied or reserved."""


def import_work_timeout_seconds() -> int:
    """Hard post-reservation deadline for every import path.

    With defaults, one call is bounded by ``4 * 120s + 3 * 60s = 11m``;
    conversion plus SBTI is 22m, then ten minutes cover quota-lock waiting,
    parsing, placement and the final commit.  The explicit deadline, rather
    than an estimated queue depth, is the authoritative upper bound.
    """
    retries = max(0, int(settings.user_llm_max_retries))
    timeout_seconds = max(1, int(settings.user_llm_timeout))
    per_call_seconds = (
        (retries + 1) * timeout_seconds
        + retries * _SDK_MAX_RETRY_AFTER_SECONDS
    )
    configured_bound = (
        _IMPORT_LLM_CALLS * per_call_seconds
        + _IMPORT_WORK_HEADROOM_SECONDS
    )
    return max(_IMPORT_WORK_TIMEOUT_FLOOR_SECONDS, configured_bound)


def _import_reservation_stale_after() -> timedelta:
    """Crash lease strictly beyond the hard work deadline.

    Once the deadline cancels a healthy worker, the existing exception path
    has ten additional minutes to roll back quota/Resident work and release the
    reservation.  If the process crashed, the next reservation attempt may
    reclaim it after the same finite deadline plus grace.
    """
    return timedelta(
        seconds=import_work_timeout_seconds() + _IMPORT_RELEASE_GRACE_SECONDS
    )


def _validate_slug(slug: str) -> str:
    clean = (slug or "").strip()
    if not clean:
        raise ValueError("Resident slug is required")
    if len(clean) > SLUG_MAX_CHARS:
        raise ValueError(f"Resident slug too long (max {SLUG_MAX_CHARS} chars)")
    return clean


def _candidate(base: str, reservation_id: str, attempt: int) -> str:
    if attempt == 0:
        return base
    token = reservation_id.replace("-", "")
    suffix_token = token[:8] if attempt == 1 else f"{token[:6]}{attempt}"
    suffix = f"-{suffix_token}"
    prefix = base[: SLUG_MAX_CHARS - len(suffix)].rstrip("-_") or "resident"
    return f"{prefix}{suffix}"


async def _reap_abandoned_reservations(db) -> None:
    """Release reservations whose owning work can no longer finish safely."""
    now = datetime.now(UTC)
    active_cutoff = now - FORGE_GENERATION_STALE_AFTER
    import_cutoff = now - _import_reservation_stale_after()
    active_statuses = {
        "routing", "routed", "running", "researching", "extracting",
        "building", "validating", "refining", "generating",
    }
    predicates = [
        ForgeSession.status.in_({"done", "error"}),
        (
            ForgeSession.status.in_(active_statuses)
            & (ForgeSession.updated_at < active_cutoff)
        ),
        (
            (ForgeSession.mode == RESERVATION_MODE)
            & (ForgeSession.status == RESERVATION_STATUS)
            & (ForgeSession.updated_at < import_cutoff)
        ),
    ]
    # A user may keep answering a guided session for longer than a generation.
    # It becomes abandoned only at the configured durable-session TTL.
    if settings.forge_session_ttl_hours > 0:
        collecting_cutoff = now - timedelta(hours=settings.forge_session_ttl_hours)
        predicates.append(
            (ForgeSession.mode == "guided")
            & (ForgeSession.status == "collecting")
            & (ForgeSession.updated_at < collecting_cutoff)
        )

    rows = (await db.execute(
        select(ForgeSession)
        .where(
            ForgeSession.target_slug.is_not(None),
            or_(*predicates),
        )
        .order_by(ForgeSession.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalars()
    for row in rows:
        if row.status in {"done", "error"}:
            row.target_slug = None
            continue
        if row.mode == "guided" and row.status == "collecting":
            row.status = "error"
            row.target_slug = None
            row.refinement_log = {
                **(row.refinement_log or {}),
                "error": "guided session expired before completion",
            }
            continue
        if row.status in active_statuses or (
            row.mode == RESERVATION_MODE and row.status == RESERVATION_STATUS
        ):
            row.target_slug = None
            if row.mode == RESERVATION_MODE:
                row.status = "expired"
            else:
                row.status = "error"
                row.refinement_log = {
                    **(row.refinement_log or {}),
                    "error": "session abandoned before slug consumption",
                }


async def _insert_if_available(db, values: dict) -> bool:
    table = ForgeSession.__table__
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(table).values(**values)
        statement = statement.on_conflict_do_nothing(index_elements=["target_slug"])
    elif dialect == "sqlite":
        statement = sqlite_insert(table).values(**values)
        statement = statement.on_conflict_do_nothing(index_elements=["target_slug"])
    else:  # The application officially supports PostgreSQL and SQLite.
        raise RuntimeError(f"Unsupported slug-reservation dialect: {dialect}")
    result = await db.execute(statement)
    return (result.rowcount or 0) == 1


async def create_reserved_forge_session(
    db,
    *,
    user_id: str,
    character_name: str,
    requested_slug: str,
    mode: str,
    status: str,
    current_stage: str,
    research_data: dict | None = None,
    extraction_data: dict | None = None,
    build_output: dict | None = None,
    validation_report: dict | None = None,
    refinement_log: dict | None = None,
    allow_suffix: bool = False,
) -> ForgeSession:
    """Insert a ForgeSession only if its final slug can be reserved.

    This function never commits. It uses dialect-native conflict-ignore rather
    than catching an IntegrityError, so a losing reservation does not poison the
    caller's quota/session transaction.
    """
    base = _validate_slug(requested_slug)
    await _reap_abandoned_reservations(db)
    reservation_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    attempts = _MAX_SUFFIX_ATTEMPTS if allow_suffix else 1
    for attempt in range(attempts):
        candidate = _candidate(base, reservation_id, attempt)
        if (await db.scalar(
            select(Resident.id).where(Resident.slug == candidate)
        )) is not None:
            continue

        values = {
            "id": reservation_id,
            "user_id": user_id,
            "character_name": character_name,
            "target_slug": candidate,
            "mode": mode,
            "status": status,
            "current_stage": current_stage,
            "research_data": dict(research_data or {}),
            "extraction_data": dict(extraction_data or {}),
            "build_output": dict(build_output or {}),
            "validation_report": dict(validation_report or {}),
            "refinement_log": dict(refinement_log or {}),
            "created_at": now,
            "updated_at": now,
        }
        if not await _insert_if_available(db, values):
            continue

        # A successful reservation insert may have waited for another
        # reservation->Resident handoff. Recheck the other unique namespace
        # after the wait, before any caller can start paid work.
        if (await db.scalar(
            select(Resident.id).where(Resident.slug == candidate)
        )) is not None:
            await db.execute(
                delete(ForgeSession).where(ForgeSession.id == reservation_id)
            )
            continue

        return (await db.execute(
            select(ForgeSession)
            .where(ForgeSession.id == reservation_id)
            .execution_options(populate_existing=True)
        )).scalar_one()

    raise SlugReservationConflict("Resident slug is already occupied or reserved")


async def reserve_slug(
    db,
    *,
    user_id: str,
    character_name: str,
    requested_slug: str,
    owner_kind: str,
    allow_suffix: bool = False,
) -> ForgeSession:
    """Create a standalone import-ready reservation; caller owns commit."""
    kind = (owner_kind or "").strip()
    if not kind or len(kind) > 50:
        raise ValueError("owner_kind must be 1-50 characters")
    return await create_reserved_forge_session(
        db,
        user_id=user_id,
        character_name=character_name,
        requested_slug=requested_slug,
        mode=RESERVATION_MODE,
        status=RESERVATION_STATUS,
        current_stage=kind,
        research_data={"reservation_kind": kind},
        allow_suffix=allow_suffix,
    )


async def consume_slug_reservation(
    db, reservation_id: str, *, user_id: str
) -> str:
    """Consume a standalone reservation; caller atomically commits Resident."""
    reservation = (await db.execute(
        select(ForgeSession)
        .where(
            ForgeSession.id == reservation_id,
            ForgeSession.user_id == user_id,
            ForgeSession.mode == RESERVATION_MODE,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if (
        reservation is None
        or reservation.status != RESERVATION_STATUS
        or not reservation.target_slug
    ):
        raise SlugReservationConflict("Slug reservation is missing or no longer active")
    slug = reservation.target_slug
    reservation.target_slug = None
    reservation.status = "consumed"
    return slug


async def release_slug_reservation(
    db, reservation_id: str, *, user_id: str
) -> bool:
    """Release a standalone reservation after rollback; caller owns commit."""
    reservation = (await db.execute(
        select(ForgeSession)
        .where(
            ForgeSession.id == reservation_id,
            ForgeSession.user_id == user_id,
            ForgeSession.mode == RESERVATION_MODE,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if reservation is None or not reservation.target_slug:
        return False
    reservation.target_slug = None
    reservation.status = "released"
    return True


def consume_session_slug(session: ForgeSession) -> str:
    """Release an already-locked Forge session reservation at terminal commit."""
    if not session.target_slug:
        raise SlugReservationConflict("Forge session has no active slug reservation")
    slug = session.target_slug
    session.target_slug = None
    return slug


def release_session_slug(session: ForgeSession) -> bool:
    """Release an actual Forge session reservation on an error path."""
    if not session.target_slug:
        return False
    session.target_slug = None
    return True
