"""Forge API — endpoints for the Skill creation pipeline (quick + deep)."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db, async_session
from app.rate_limit import limiter
from app.schemas.forge import (
    ForgeStartRequest, ForgeStartResponse,
    ForgeAnswerRequest, ForgeAnswerResponse,
    ForgeStatusResponse,
    DeepStartRequest, DeepStartResponse, DeepStatusResponse,
)
from app.services.auth_service import get_current_user
from app.forge.legacy_sessions import (
    ForgeSessionNotFound,
    ForgeSessionStateError,
    get_status,
    start_forge,
    start_quick_forge,
    submit_answer,
    will_generate,
)
from app.forge.legacy_pipeline import run_generation_pipeline, run_quick_pipeline
from app.forge.runtime_limits import (
    FORGE_GENERATION_STALE_AFTER,
    FORGE_PIPELINE_TIMEOUT_S,
)
from app.forge.pipeline import (
    ForgeBudgetExceeded,
    ForgeInputError,
    FORGE_INPUT_MAX_CHARS,
    ForgePipeline,
    ForgeSlugConflict,
    TERMINAL_STATUSES as _TERMINAL_STATUSES,
    validate_inputs,
)
from app.llm.client import get_client as get_llm_client
from app.llm.budget import forge_blocked
from app.models.forge_session import ForgeSession
from app.services.ugc_creation_quota import (
    DailyCreationLimitExceeded,
    error_detail as quota_error_detail,
)
from app.services.slug_reservation import (
    SlugReservationConflict,
    release_session_slug,
)

router = APIRouter(prefix="/forge", tags=["forge"])


async def _require_auth(request: Request, db: AsyncSession = Depends(get_db)):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    return user


@router.post("/start", response_model=ForgeStartResponse)
@limiter.limit(lambda: f"{settings.rest_rate_limit_forge_per_minute}/minute")
async def forge_start(
    request: Request,
    req: ForgeStartRequest,
    db: AsyncSession = Depends(get_db),
):
    user = await _require_auth(request, db)
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    if len(req.name) > 100:
        raise HTTPException(status_code=400, detail="Name too long (max 100 chars)")
    if await forge_blocked(db, user.id):
        raise HTTPException(status_code=402, detail="Daily LLM budget reached — try again later")
    try:
        result = await start_forge(db, user.id, req.name.strip())
    except DailyCreationLimitExceeded as exc:
        raise HTTPException(status_code=429, detail=quota_error_detail(exc)) from exc
    except SlugReservationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ForgeStartResponse(**result)


@router.post("/answer", response_model=ForgeAnswerResponse)
@limiter.limit(lambda: f"{settings.rest_rate_limit_forge_per_minute}/minute")
async def forge_answer(
    req: ForgeAnswerRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await _require_auth(request, db)
    if not req.answer.strip():
        raise HTTPException(status_code=400, detail="Answer cannot be empty")
    if len(req.answer) > FORGE_INPUT_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Answer too long (max {FORGE_INPUT_MAX_CHARS} chars)",
        )
    try:
        if await will_generate(db, req.forge_id, user.id):
            if await forge_blocked(db, user.id):
                raise HTTPException(
                    status_code=402,
                    detail="Daily LLM budget reached — try again later",
                )
        result = await submit_answer(db, req.forge_id, user.id, req.answer.strip())
    except ForgeSessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ForgeSessionStateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Trigger LLM generation in background after final answer
    if result["next_step"] is None:
        _spawn_bg(_run_legacy_pipeline_bg(req.forge_id, run_generation_pipeline))

    return ForgeAnswerResponse(**result)


class QuickForgeRequest(BaseModel):
    name: str
    raw_text: str   # free-form text about the person — biography, chat logs, descriptions, etc.


@router.post("/quick")
@limiter.limit(lambda: f"{settings.rest_rate_limit_forge_per_minute}/minute")
async def forge_quick(
    request: Request,
    req: QuickForgeRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    One-shot forge: provide a name + raw text, the system extracts all three layers.
    Persists the session and runs generation after the response is sent.
    """
    user = await _require_auth(request, db)
    if not req.name.strip():
        raise HTTPException(status_code=400, detail="Name cannot be empty")
    if not req.raw_text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty")
    try:
        validate_inputs(req.name, req.raw_text)
    except ForgeInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if await forge_blocked(db, user.id):
        raise HTTPException(status_code=402, detail="Daily LLM budget reached — try again later")

    try:
        forge_session = await start_quick_forge(
            db, user.id, req.name.strip(), req.raw_text.strip()
        )
    except DailyCreationLimitExceeded as exc:
        raise HTTPException(status_code=429, detail=quota_error_detail(exc)) from exc
    except SlugReservationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    forge_id = forge_session.id

    # Use starlette Response + background to ensure task runs after response
    from starlette.responses import JSONResponse
    from starlette.background import BackgroundTask
    import logging

    async def _run():
        logging.warning(f"[FORGE] Background task STARTED for {forge_id}")
        await _run_legacy_pipeline_bg(forge_id, run_quick_pipeline)
        logging.warning(f"[FORGE] Background task COMPLETED for {forge_id}")

    return JSONResponse(
        content={"forge_id": forge_id, "status": "generating"},
        background=BackgroundTask(_run),
    )


@router.get("/status/{forge_id}", response_model=ForgeStatusResponse)
async def forge_status(
    forge_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    user = await _require_auth(request, db)
    try:
        result = await get_status(db, forge_id, user.id)
    except ForgeSessionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ForgeStatusResponse(**result)


# ── Deep forge (pipeline) endpoints ──────────────────────────────────

import logging
from datetime import datetime, UTC

logger = logging.getLogger(__name__)

_DEEP_STAGE_ALIASES = {
    "pending": "routing",
    "router": "routing",
    "routing": "routing",
    "routed": "routing",
    "running": "routing",
    "research": "researching",
    "researching": "researching",
    "extraction": "extracting",
    "extracting": "extracting",
    "build": "building",
    "building": "building",
    "validation": "validating",
    "validating": "validating",
    "refinement": "refining",
    "refining": "refining",
    "done": "done",
    "error": "error",
}
_DEEP_STAGE_PROGRESS = {
    "routing": 10,
    "researching": 25,
    "extracting": 45,
    "building": 60,
    "validating": 75,
    "refining": 90,
    "done": 100,
    "error": 100,
}


def _deep_stage(session: ForgeSession) -> str:
    """Return one stable UI stage while preserving raw status separately."""
    if session.status in _TERMINAL_STATUSES:
        return session.status
    return (
        _DEEP_STAGE_ALIASES.get(session.status)
        or _DEEP_STAGE_ALIASES.get(session.current_stage)
        or "routing"
    )

# Strong references to fire-and-forget tasks: asyncio only keeps weak refs, so
# an unreferenced task can be garbage-collected mid-await and silently vanish —
# exactly the "stuck in building forever" failure mode (P1 fix).
_BG_TASKS: set = set()


def _spawn_bg(coro) -> None:
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def _mark_session_error(session_id: str, message: str) -> None:
    """Force a forge session into a terminal error state (own fresh DB session)."""
    try:
        async with async_session() as db:
            result = await db.execute(
                select(ForgeSession).where(ForgeSession.id == session_id)
            )
            session = result.scalar_one_or_none()
            if session and session.status not in _TERMINAL_STATUSES:
                session.status = "error"
                release_session_slug(session)
                session.refinement_log = {
                    **(session.refinement_log or {}), "error": message,
                }
                await db.commit()
    except Exception:
        logger.error("failed to mark forge session %s as error", session_id, exc_info=True)


async def _run_pipeline_bg(session_id: str):
    """Background task: run forge pipeline with its own DB session.

    Wrapped in an overall timeout + last-resort error marker so the session
    ALWAYS reaches a terminal status (done/error) no matter how the pipeline
    dies (P1 fix: sessions used to sit in "building" forever).
    """
    try:
        async with async_session() as bg_db:
            system_client = get_llm_client("system")
            user_client = get_llm_client("user")
            pipeline = ForgePipeline(
                db=bg_db, system_client=system_client, user_client=user_client,
            )
            await asyncio.wait_for(
                pipeline.run_to_completion(session_id),
                timeout=FORGE_PIPELINE_TIMEOUT_S,
            )
    except asyncio.TimeoutError:
        logger.error("forge pipeline %s timed out after %ss", session_id, FORGE_PIPELINE_TIMEOUT_S)
        await _mark_session_error(session_id, f"pipeline timed out after {FORGE_PIPELINE_TIMEOUT_S}s")
    except Exception as e:
        logger.error("forge pipeline %s crashed: %s", session_id, e, exc_info=True)
        await _mark_session_error(session_id, str(e))


async def _run_legacy_pipeline_bg(session_id: str, runner) -> None:
    """Give legacy guided/quick runs the same hard terminal timeout as deep."""
    try:
        async with async_session() as bg_db:
            await asyncio.wait_for(
                runner(session_id, bg_db), timeout=FORGE_PIPELINE_TIMEOUT_S
            )
    except asyncio.TimeoutError:
        logger.error(
            "legacy forge pipeline %s timed out after %ss",
            session_id,
            FORGE_PIPELINE_TIMEOUT_S,
        )
        await _mark_session_error(
            session_id, f"pipeline timed out after {FORGE_PIPELINE_TIMEOUT_S}s"
        )
    except Exception as exc:
        logger.error(
            "legacy forge pipeline %s crashed: %s", session_id, exc, exc_info=True
        )
        await _mark_session_error(session_id, str(exc))


@router.post("/deep-start", response_model=DeepStartResponse)
@limiter.limit(lambda: f"{settings.rest_rate_limit_forge_per_minute}/minute")
async def deep_start(
    req: DeepStartRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Start a deep-forge pipeline session (routes to quick or deep automatically)."""
    user = await _require_auth(request, db)
    if not req.character_name.strip():
        raise HTTPException(status_code=400, detail="character_name is required")
    if await forge_blocked(db, user.id):
        raise HTTPException(status_code=402, detail="Daily LLM budget reached — try again later")

    system_client = get_llm_client("system")
    user_client = get_llm_client("user")

    pipeline = ForgePipeline(
        db=db, system_client=system_client, user_client=user_client,
    )
    try:
        session = await pipeline.start(
            user_id=user.id,
            character_name=req.character_name.strip(),
            raw_text=req.raw_text,
            user_material=req.user_material,
        )
    except ForgeSlugConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ForgeInputError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except DailyCreationLimitExceeded as exc:
        raise HTTPException(status_code=429, detail=quota_error_detail(exc)) from exc
    except ForgeBudgetExceeded as exc:
        raise HTTPException(status_code=402, detail=str(exc)) from exc

    # Launch the remainder of the pipeline in a background task (strong ref —
    # a bare create_task can be GC'd mid-flight and silently die)
    _spawn_bg(_run_pipeline_bg(session.id))

    return DeepStartResponse(
        forge_id=session.id,
        mode=session.mode,
        status=session.status,
    )


@router.get("/deep-status/{forge_id}", response_model=DeepStatusResponse)
async def deep_status(
    forge_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Check the status of a deep-forge pipeline session."""
    user = await _require_auth(request, db)

    result = await db.execute(
        select(ForgeSession).where(
            ForgeSession.id == forge_id,
            ForgeSession.user_id == user.id,
            ForgeSession.mode.in_(("pending", "quick", "deep")),
        )
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Forge session not found")

    # Lazy stale sweep (P1 fix): if the background task died without writing a
    # terminal state (process restart, task GC'd), a poll after the staleness
    # window flips the session to error instead of showing "building" forever.
    if session.status not in _TERMINAL_STATUSES and session.updated_at is not None:
        updated_at = session.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        if datetime.now(UTC) - updated_at > FORGE_GENERATION_STALE_AFTER:
            session.status = "error"
            release_session_slug(session)
            session.refinement_log = {
                **(session.refinement_log or {}),
                "error": f"session stalled in '{session.current_stage}' — swept by staleness check",
            }
            await db.commit()
            await db.refresh(session)

    stage = _deep_stage(session)
    build = session.build_output or {}
    result_data = session.validation_report or {}
    error = (session.refinement_log or {}).get("error")
    return DeepStatusResponse(
        forge_id=session.id,
        status=session.status,
        current_stage=session.current_stage,
        mode=session.mode,
        character_name=session.character_name,
        stage=stage,
        progress=_DEEP_STAGE_PROGRESS[stage],
        name=session.character_name,
        ability_md=build.get("ability_md"),
        persona_md=build.get("persona_md"),
        soul_md=build.get("soul_md"),
        star_rating=int(result_data.get("star_rating") or 0),
        district=str(result_data.get("district") or ""),
        resident_id=result_data.get("resident_id"),
        error=str(error) if error else None,
    )
