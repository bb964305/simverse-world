"""F3 卸任财政审计 — a read-only fiscal summary of an official's term.

Written when an office is vacated (term expiry today; F2's revocation wires
in at 收口). The record NEVER touches an account: every number here is a
SELECT, and the only write is this module's own ``system_config`` row.

The town has no ledger table by design — ``transactions.user_id`` is a hard
``users.id`` FK, so a synthetic town account cannot be a ledger row (see
``app/models/town_treasury.py``). The auditable surface is therefore exactly
what S1-5 left behind (balances + ``updated_at`` + the ``town_last_spend_at``
stamp) plus the S2-5 fiscal policy rows and the civic polls that moved them.

Storage is a ``system_config`` row (group ``office_audit``): F3 ships no
migration this batch, and "scalar policy state lives in system_config" is the
established S1-5 pattern. ``system_config.value`` is ``String(2000)`` and
``ConfigService.set`` serializes with ``json.dumps`` (ensure_ascii=True → one
CJK char costs 6 bytes), so every payload goes through :func:`_fit` first.

Fail-open throughout: an audit hiccup must never break the vacate that
triggered it.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.office import Office
from app.models.system_config import SystemConfig

logger = logging.getLogger(__name__)

#: system_config group every term-audit row is filed under.
AUDIT_GROUP = "office_audit"
#: key shape — ``office_audit:<office_key>:<slug[:60]>:<YYYYmmddTHHMMSS>``.
AUDIT_KEY_PREFIX = "office_audit"
#: payload shape version (bump when a field changes meaning).
AUDIT_SCHEMA_VERSION = 1
#: json.dumps ceiling; system_config.value is String(2000) — leave headroom.
_VALUE_LIMIT = 1900
#: caps on the two unbounded lists in the payload.
_MAX_POLICY_CHANGES = 12
_MAX_POLL_QUESTIONS = 3
#: per-field truncation so one pathological row cannot eat the whole budget.
_MAX_VALUE_CHARS = 64
_MAX_QUESTION_CHARS = 60


def _as_utc(dt: datetime | None) -> datetime | None:
    """UTC-aware coercion. Naive datetimes are assumed UTC (how the DB stores
    them), matching ``world_clock._as_zone``'s assumption."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


async def _rollback_quietly(db: AsyncSession) -> None:
    """Roll back after a swallowed failure so the caller's session stays
    usable. Never raises — a fail-open path may not explode on its way out."""
    try:
        await db.rollback()
    except Exception:
        logger.warning("rollback after a swallowed audit failure also failed",
                       exc_info=True)


def audit_key(
    office_key: str, holder_slug: str, term_started_at: datetime | None,
) -> str:
    """The system_config key for one term audit. ``SystemConfig.key`` is
    ``String(200)``; the slug is clamped so a 100-char slug cannot overflow
    it (the full slug still travels inside the payload)."""
    stamp = (_as_utc(term_started_at) or datetime.now(UTC)).strftime(
        "%Y%m%dT%H%M%S")
    return f"{AUDIT_KEY_PREFIX}:{office_key}:{holder_slug[:60]}:{stamp}"[:200]


def _fit(payload: dict) -> dict:
    """Shrink the payload until ``json.dumps`` fits system_config.value.

    Measured exactly the way ``ConfigService.set`` will serialize it (default
    ``ensure_ascii=True``) — the Chinese poll questions are the part that
    actually blows the 2000-char column, and they cost 6 chars each escaped.
    """
    out = dict(payload)
    changes = list(out.get("fiscal_policy_changes") or [])
    questions = list(out.get("fiscal_poll_questions") or [])
    truncated = bool(out.get("truncated"))
    if len(changes) > _MAX_POLICY_CHANGES:
        changes, truncated = changes[:_MAX_POLICY_CHANGES], True
    if len(questions) > _MAX_POLL_QUESTIONS:
        questions, truncated = questions[:_MAX_POLL_QUESTIONS], True
    out["fiscal_policy_changes"] = changes
    out["fiscal_poll_questions"] = questions
    out["truncated"] = truncated
    while len(json.dumps(out)) > _VALUE_LIMIT:
        if out["fiscal_poll_questions"]:
            out["fiscal_poll_questions"] = out["fiscal_poll_questions"][:-1]
        elif out["fiscal_policy_changes"]:
            out["fiscal_policy_changes"] = out["fiscal_policy_changes"][:-1]
        else:
            break
        out["truncated"] = True
    return out


async def _fiscal_policy_changes(
    db: AsyncSession, started: datetime | None, ended: datetime,
) -> list[dict]:
    """S2-5 fiscal policy rows whose ``updated_at`` falls inside the term.

    Fail-open on a world whose ``policies`` table predates S2-5.
    """
    from app.models.policy import Policy
    from app.services.policy_service import FISCAL_POLICY_KEYS
    try:
        rows = (await db.execute(
            select(Policy).where(Policy.key.in_(sorted(FISCAL_POLICY_KEYS)))
        )).scalars().all()
    except Exception:
        logger.warning("office audit: policies unreadable", exc_info=True)
        return []
    out: list[dict] = []
    for p in rows:
        ts = _as_utc(p.updated_at)
        if ts is None or ts > ended:
            continue
        if started is not None and ts < started:
            continue
        out.append({
            "key": p.key,
            "value": str(p.value)[:_MAX_VALUE_CHARS],
            "version": int(p.version or 0),
            "updated_by": (p.updated_by or "")[:_MAX_VALUE_CHARS],
            "updated_at": ts.isoformat(),
        })
    out.sort(key=lambda e: e["updated_at"])
    return out


async def _fiscal_polls(
    db: AsyncSession, started: datetime | None, ended: datetime,
) -> tuple[int, list[str]]:
    """Civic polls closed inside the term whose WINNING option carried a fiscal
    effect. Returns (count, up to _MAX_POLL_QUESTIONS questions).

    Only the winner counts, and that is not pedantry: a fiscal referendum is
    shaped ``[{"label":"赞成","effect":{...}}, {"label":"反对","effect":None}]``
    (``policy_service._open_amend_poll``), so scanning *every* option would
    count每一次被否决的加税提案 as fiscal activity — a line reading
    「任内经公决通过的财政议案 = 3」 would then be true of a mayor who never
    passed a single one. ``civic_service._close_one`` stamps
    ``opts[win]["won"] = True`` on the executed option (and deliberately does
    NOT stamp it when a tier-governed poll 流会), which is the same marker
    ``routers/townhall.py`` already reads — so "did it actually pass" is one
    predicate, not a guess.
    """
    from app.models.season import Poll
    from app.services.policy_service import FISCAL_POLICY_KEYS
    try:
        rows = (await db.execute(
            select(Poll).where(Poll.status == "closed")
        )).scalars().all()
    except Exception:
        logger.warning("office audit: polls unreadable", exc_info=True)
        return 0, []
    hits: list[str] = []
    for p in rows:
        ts = _as_utc(p.closes_at)
        if ts is None or ts > ended:
            continue
        if started is not None and ts < started:
            continue
        for opt in (p.options_json or []):
            if not (opt or {}).get("won"):
                continue
            effect = (opt or {}).get("effect") or {}
            if (effect.get("type") in ("policy", "system_config")
                    and effect.get("key") in FISCAL_POLICY_KEYS):
                hits.append(str(p.question or "")[:_MAX_QUESTION_CHARS])
                break
    return len(hits), hits[:_MAX_POLL_QUESTIONS]


async def collect_fiscal_audit(
    db: AsyncSession, *,
    office_key: str,
    holder_slug: str,
    term_started_at: datetime | None,
    term_ended_at: datetime | None = None,
) -> dict:
    """Read-only fiscal summary of ``holder_slug``'s term in ``office_key``.

    Pure SELECTs — this function must never write. ``term_ended_at`` defaults
    to now (the vacate instant).
    """
    from app import world_clock
    from app.config import settings
    from app.models.town_treasury import TOWN_KEY, TownTreasury
    from app.services import coin_service, treasury_service
    from app.services.config_service import ConfigService

    started = _as_utc(term_started_at)
    ended = _as_utc(term_ended_at) or datetime.now(UTC)

    strategy = (await db.execute(
        select(Office.fill_strategy).where(Office.office_key == office_key)
    )).scalar_one_or_none() or ""

    town_balance = await treasury_service.balance(db)
    town_updated = _as_utc((await db.execute(
        select(TownTreasury.updated_at).where(TownTreasury.key == TOWN_KEY)
    )).scalar_one_or_none())

    try:
        last_spend = await ConfigService(db).get(treasury_service.LAST_SPEND_KEY)
    except Exception:
        logger.warning("office audit: town_last_spend_at unreadable",
                       exc_info=True)
        last_spend = None

    holder_balance = await coin_service.treasury_balance(db, holder_slug)

    term_world_days = None
    if started is not None:
        try:
            span = (world_clock.real_to_world(ended)
                    - world_clock.real_to_world(started))
            term_world_days = round(span.total_seconds() / 86400.0, 3)
        except Exception:
            logger.warning("office audit: world-day conversion failed",
                           exc_info=True)

    changes = await _fiscal_policy_changes(db, started, ended)
    polls_passed, poll_questions = await _fiscal_polls(db, started, ended)

    return _fit({
        "schema_version": AUDIT_SCHEMA_VERSION,
        "office_key": office_key,
        "fill_strategy": strategy,
        "holder_slug": holder_slug,
        "term_started_at": started.isoformat() if started else None,
        "term_ended_at": ended.isoformat(),
        "term_world_days": term_world_days,
        "town_balance_sc_end": int(town_balance),
        "town_treasury_updated_at": (
            town_updated.isoformat() if town_updated else None),
        "town_last_spend_at": last_spend if isinstance(last_spend, str) else None,
        "holder_balance_sc_end": int(holder_balance),
        "mayor_wage_multiplier": (
            float(settings.election_mayor_wage_bonus)
            if office_key == "mayor" and settings.election_enabled else 1.0
        ),
        "fiscal_policy_changes": changes,
        # 名字与语义严格对齐:计的是「任内经公决**通过**的财政议案」,
        # 不是「任内出现过的财政议案」——审计要的是前者。
        "fiscal_polls_passed": polls_passed,
        "fiscal_poll_questions": poll_questions,
        "generated_at": datetime.now(UTC).isoformat(),
    })


async def record_term_audit(
    db: AsyncSession, *,
    office_key: str,
    holder_slug: str | None,
    term_started_at: datetime | None,
    term_ended_at: datetime | None = None,
) -> dict | None:
    """Collect + persist one term audit; returns the stored payload.

    None means there was nothing to audit (no holder) or the write failed —
    fail-open, because an audit must never turn a completed vacate into an
    exception.

    F2 contract: call this on the revocation path AFTER the office row was
    vacated, passing the holder and ``term_started_at`` read BEFORE the UPDATE
    (``OfficeService.vacate(..., audit=True)`` does exactly that for you).

    CALLER CONTRACT on a ``None`` return (failure path): ``_rollback_quietly``
    calls ``db.rollback()``, and ``Session.rollback()`` expires the ENTIRE
    identity map (``dirty_only=False``) — not just objects this function
    touched. Empirically reproduced: a caller that loaded an ORM object
    earlier in the same session (e.g. the just-vacated ``Office`` row) and
    then does a plain synchronous attribute read on it AFTER a failed call
    here raises ``sqlalchemy.exc.MissingGreenlet`` (the lazy reload the
    expired attribute triggers has no greenlet context to run in). This is
    the same failure class flagged on the sister F2 line, just triggered by
    a genuine exception path rather than a guarded-UPDATE rowcount branch —
    which is why the fix there (``begin_nested()`` savepoints, narrower
    ``dirty_only=True`` rollback scope) does not apply here: by the time
    ``ConfigService.set``'s own commit can raise, the outer transaction/
    connection may already be broken, so a plain ``db.rollback()`` is the
    only thing that reliably resets it. The safe caller pattern instead:
    after a ``None`` return, either don't touch previously-loaded objects
    again without ``await db.refresh(obj)`` first, or re-``SELECT`` them.
    """
    if not office_key or not holder_slug:
        return None
    try:
        payload = await collect_fiscal_audit(
            db, office_key=office_key, holder_slug=holder_slug,
            term_started_at=term_started_at, term_ended_at=term_ended_at,
        )
        from app.services.config_service import ConfigService
        await ConfigService(db).set(
            audit_key(office_key, holder_slug, term_started_at),
            payload, group=AUDIT_GROUP, updated_by="office_term_audit",
        )
        return payload
    except Exception:
        logger.warning("office term audit failed for %s/%s",
                       office_key, holder_slug, exc_info=True)
        # Fail-open has to cover the SESSION, not just the return value.
        # ConfigService.set writes and commits, so the exception can come out
        # of a flush/commit and leave the session in the needs-rollback state —
        # every LATER statement would then raise PendingRollbackError. That is
        # exactly the half-finished shape §4.3 forbids: F2 calls
        # ``vacate("mayor", audit=True)``, gets True back, and then does
        # 改档位 → 写历史行 → 断言 → 广播 on the same session.
        # (The three inner try blocks inside collect_fiscal_audit are pure
        # SELECTs and stay as they are.)
        await _rollback_quietly(db)
        return None


async def list_term_audits(
    db: AsyncSession, *, office_key: str | None = None, limit: int = 20,
) -> list[dict]:
    """Stored term audits, newest term-end first. Read-only."""
    try:
        rows = (await db.execute(
            select(SystemConfig).where(SystemConfig.group == AUDIT_GROUP)
        )).scalars().all()
    except Exception:
        logger.warning("office term audit listing failed", exc_info=True)
        return []
    out: list[dict] = []
    for row in rows:
        if not str(row.key or "").startswith(f"{AUDIT_KEY_PREFIX}:"):
            continue
        try:
            payload = json.loads(row.value)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if office_key and payload.get("office_key") != office_key:
            continue
        out.append(payload)
    out.sort(key=lambda p: str(p.get("term_ended_at") or ""), reverse=True)
    return out[:limit]
