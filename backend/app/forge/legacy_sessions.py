"""Durable legacy guided/quick Forge sessions.

The original module-level dict made every request worker-affine and lost all
state on restart.  Legacy state now uses the existing ``forge_sessions`` table:
answers/step live in ``research_data``, generated layers in ``build_output``,
and result metadata in ``validation_report``. Public reads always bind owner.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.config import settings
from app.forge.legacy_prompts import FORGE_QUESTIONS
from app.forge.runtime_limits import FORGE_GENERATION_STALE_AFTER
from app.models.forge_session import ForgeSession
from app.services.ugc_creation_quota import claim_creation_slot
from app.services.resident_placement import _generate_slug
from app.services.slug_reservation import (
    create_reserved_forge_session,
    release_session_slug,
)


LEGACY_MODES = frozenset({"guided", "legacy_quick"})


class ForgeSessionNotFound(ValueError):
    pass


class ForgeSessionStateError(ValueError):
    pass


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _is_expired(session: ForgeSession) -> bool:
    ttl = settings.forge_session_ttl_hours
    if ttl <= 0:
        return False
    anchor = session.updated_at or session.created_at
    return datetime.now(UTC) - _aware(anchor) > timedelta(hours=ttl)


async def _owned_session(
    db, forge_id: str, user_id: str, *, for_update: bool = False
) -> ForgeSession:
    stmt = select(ForgeSession).where(
        ForgeSession.id == forge_id,
        ForgeSession.user_id == user_id,
        ForgeSession.mode.in_(LEGACY_MODES),
    )
    if for_update:
        stmt = stmt.with_for_update()
    session = (await db.execute(stmt)).scalar_one_or_none()
    # Return the same 404 for absent and foreign IDs: no ownership oracle.
    if session is None:
        raise ForgeSessionNotFound("Forge session not found")
    # In-flight rows must remain observable long enough for get_status() to
    # persist the stale-run terminal error, even when the first poll is after
    # the general session TTL.
    if _is_expired(session) and session.status not in {"generating", "running"}:
        raise ForgeSessionNotFound("Forge session not found")
    return session


def _research(session: ForgeSession) -> dict:
    return dict(session.research_data or {})


def legacy_state(session: ForgeSession) -> dict:
    research = _research(session)
    build = dict(session.build_output or {})
    result = dict(session.validation_report or {})
    error = (session.refinement_log or {}).get("error")
    return {
        "forge_id": session.id,
        "user_id": session.user_id,
        "status": session.status,
        "step": int(research.get("step", 1)),
        "name": session.character_name,
        "answers": dict(research.get("answers") or {"1": session.character_name}),
        "ability_md": str(build.get("ability_md") or ""),
        "persona_md": str(build.get("persona_md") or ""),
        "soul_md": str(build.get("soul_md") or ""),
        "star_rating": int(result.get("star_rating") or 0),
        "district": str(result.get("district") or ""),
        "resident_id": result.get("resident_id"),
        "error": str(error) if error else None,
    }


def apply_legacy_state(session: ForgeSession, state: dict) -> None:
    session.status = state["status"]
    session.current_stage = str(state.get("current_stage") or session.current_stage or "")
    session.research_data = {
        **_research(session),
        "step": int(state.get("step", 1)),
        "answers": dict(state.get("answers") or {}),
    }
    session.build_output = {
        "ability_md": str(state.get("ability_md") or ""),
        "persona_md": str(state.get("persona_md") or ""),
        "soul_md": str(state.get("soul_md") or ""),
    }
    session.validation_report = {
        **(session.validation_report or {}),
        "star_rating": int(state.get("star_rating") or 0),
        "district": str(state.get("district") or ""),
        "resident_id": state.get("resident_id"),
    }
    session.refinement_log = {
        **(session.refinement_log or {}),
        "error": state.get("error"),
    }


async def _create(
    db,
    *,
    user_id: str,
    name: str,
    mode: str,
    status: str,
    step: int,
    answers: dict[str, str],
) -> ForgeSession:
    try:
        session = await create_reserved_forge_session(
            db,
            user_id=user_id,
            character_name=name,
            requested_slug=_generate_slug(name),
            mode=mode,
            status=status,
            current_stage="collecting" if status == "collecting" else "build",
            research_data={"step": step, "answers": answers},
            allow_suffix=True,
        )
        # Match the terminal transaction's ForgeSession -> User lock order.
        await claim_creation_slot(db, user_id)
    except Exception:
        await db.rollback()
        raise
    await db.commit()  # quota claim + durable session, one transaction
    await db.refresh(session)
    return session


async def start_forge(db, user_id: str, name: str) -> dict:
    session = await _create(
        db,
        user_id=user_id,
        name=name,
        mode="guided",
        status="collecting",
        step=1,
        answers={"1": name},
    )
    return {"forge_id": session.id, "step": 1, "question": FORGE_QUESTIONS[2]}


async def start_quick_forge(db, user_id: str, name: str, raw_text: str) -> ForgeSession:
    return await _create(
        db,
        user_id=user_id,
        name=name,
        mode="legacy_quick",
        status="generating",
        step=5,
        answers={"1": name, "2": raw_text},
    )


async def will_generate(db, forge_id: str, user_id: str) -> bool:
    session = await _owned_session(db, forge_id, user_id)
    state = legacy_state(session)
    return session.status == "collecting" and state["step"] + 1 >= 5


async def submit_answer(db, forge_id: str, user_id: str, answer: str) -> dict:
    session = await _owned_session(db, forge_id, user_id, for_update=True)
    state = legacy_state(session)
    if session.status != "collecting":
        raise ForgeSessionStateError(f"Session is in '{session.status}' state")

    current_step = state["step"] + 1
    state["answers"][str(current_step)] = answer
    state["step"] = current_step
    if current_step >= 5:
        state["status"] = "generating"
        state["current_stage"] = "ability"
        next_step = None
        question = None
    else:
        state["status"] = "collecting"
        next_step = current_step + 1
        question = FORGE_QUESTIONS[next_step]
    apply_legacy_state(session, state)
    await db.commit()
    return {
        "forge_id": forge_id,
        "step": current_step,
        "next_step": next_step,
        "question": question,
        "ability_md": None,
        "persona_md": None,
        "soul_md": None,
    }


async def get_status(db, forge_id: str, user_id: str) -> dict:
    session = await _owned_session(db, forge_id, user_id)
    if (
        session.status in {"generating", "running"}
        and session.updated_at is not None
        and datetime.now(UTC) - _aware(session.updated_at) > FORGE_GENERATION_STALE_AFTER
    ):
        stalled = session.current_stage or session.status
        session.status = "error"
        release_session_slug(session)
        session.refinement_log = {
            **(session.refinement_log or {}),
            "error": f"session stalled in '{stalled}' — swept by staleness check",
        }
        await db.commit()
        await db.refresh(session)
    return legacy_state(session)


async def load_internal(db, forge_id: str) -> ForgeSession | None:
    """Load a legacy session for its trusted background runner."""
    stmt = select(ForgeSession).where(
        ForgeSession.id == forge_id, ForgeSession.mode.in_(LEGACY_MODES)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def claim_internal_run(db, forge_id: str) -> ForgeSession | None:
    """Atomically give one worker execution authority for a legacy run."""
    claimed = await db.execute(
        update(ForgeSession)
        .where(
            ForgeSession.id == forge_id,
            ForgeSession.mode.in_(LEGACY_MODES),
            ForgeSession.status == "generating",
        )
        .values(status="running")
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if (claimed.rowcount or 0) != 1:
        return None
    return (await db.execute(
        select(ForgeSession)
        .where(ForgeSession.id == forge_id)
        .execution_options(populate_existing=True)
    )).scalar_one()


async def lock_internal_completion(db, forge_id: str) -> ForgeSession | None:
    """Fence a claimed runner immediately before its terminal transaction."""
    return (await db.execute(
        select(ForgeSession)
        .where(
            ForgeSession.id == forge_id,
            ForgeSession.mode.in_(LEGACY_MODES),
            ForgeSession.status == "running",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
