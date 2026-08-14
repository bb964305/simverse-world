import asyncio
import logging
import random
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.config import settings
from app.rate_limit import limiter
from app.models.resident import Resident
from app.models.user import User
from app.schemas.resident import ResidentListItem, ResidentDetail, ResidentEditRequest, VersionSnapshot, ResidentImportResponse, PlayerPositionUpdate
from app.services.resident_service import list_residents, get_resident_by_slug
from app.services.version_service import create_version_snapshot, get_versions
from app.services.auth_service import get_current_user
from app.services.scoring_service import compute_star_rating
from app.services.sbti_service import compute_sbti, update_meta_with_sbti
from app.services.skill_import_service import (
    IMPORT_MAX_UPLOAD_BYTES,
    SkillFormat,
    SkillImportValidationError,
    convert_to_standard,
    detect_skill_format,
    parse_skill_zip,
    validate_import_identity,
    validate_import_layers,
)
from app.services.resident_placement import allocate_resident_location, _generate_slug, SPRITE_KEYS
from app.services.civic_membership import UGC_RESIDENT_TYPE
from app.services.ugc_creation_quota import (
    DailyCreationLimitExceeded,
    claim_creation_slot,
    error_detail as creation_limit_detail,
)
from app.services.slug_reservation import (
    SlugReservationConflict,
    consume_slug_reservation,
    import_work_timeout_seconds,
    release_slug_reservation,
    reserve_slug,
)
from app.llm.budget import forge_blocked

router = APIRouter(prefix="/residents", tags=["residents"])
logger = logging.getLogger(__name__)


async def _require_user_auth(request: Request, db: AsyncSession = Depends(get_db)):
    """Extract and verify auth — returns user object."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user


@router.get("", response_model=list[ResidentListItem])
async def list_all(
    db: AsyncSession = Depends(get_db),
    limit: int | None = Query(None, ge=1, le=500, description="page size; omit for the full roster"),
    offset: int = Query(0, ge=0),
    exclude_players: bool = Query(False, description="opt-in: filter out player avatars (NPC layer)"),
):
    residents = await list_residents(db, limit=limit, offset=offset, exclude_players=exclude_players)
    return [ResidentListItem.model_validate(r, from_attributes=True) for r in residents]


@router.get("/{slug}/goals")
async def resident_goals(slug: str, db: AsyncSession = Depends(get_db)):
    """A1: a resident's active life goal + recent resolved goals (public)."""
    from fastapi import HTTPException
    from app.services.goal_service import get_goals
    resident = await get_resident_by_slug(db, slug)
    if not resident:
        raise HTTPException(status_code=404, detail="Resident not found")
    return await get_goals(db, resident.id)


# ── C1 soul card: card / export / import ─────────────────────────────

CARD_SCHEMA_VERSION = 1


def _raise_import_validation(exc: SkillImportValidationError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


async def _claim_import_slot(db: AsyncSession, user_id: str) -> None:
    try:
        await claim_creation_slot(db, user_id)
    except DailyCreationLimitExceeded as exc:
        raise HTTPException(status_code=429, detail=creation_limit_detail(exc)) from exc


async def _reserve_import_slug(
    db: AsyncSession,
    *,
    user_id: str,
    name: str,
    slug: str,
    owner_kind: str,
    allow_suffix: bool,
):
    """Persist a cross-worker slug claim before any paid/placement work."""
    try:
        reservation = await reserve_slug(
            db,
            user_id=user_id,
            character_name=name,
            requested_slug=slug,
            owner_kind=owner_kind,
            allow_suffix=allow_suffix,
        )
        # The reservation must be visible to other workers while this request
        # performs conversion/SBTI. UGC quota is deliberately claimed only in
        # the following transaction so a failed import does not consume it.
        await db.commit()
        return reservation
    except SlugReservationConflict as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except BaseException:
        try:
            await db.rollback()
        except BaseException:
            # Preserve cancellation/the original failure. No committed
            # reservation exists on this path.
            pass
        raise


async def _release_import_slug_after_failure(
    db: AsyncSession, reservation_id: str, user_id: str
) -> None:
    """Best-effort release after rolling back quota/Resident work.

    Cancellation and connection failures may still interrupt cleanup; the
    reservation service's stale sweep is the durable fallback for that case.
    """
    try:
        await db.rollback()
    except BaseException:
        logger.warning("failed to roll back import before slug release", exc_info=True)
    try:
        await release_slug_reservation(db, reservation_id, user_id=user_id)
        await db.commit()
    except BaseException:
        try:
            await db.rollback()
        except BaseException:
            pass
        logger.warning(
            "failed to release import slug reservation %s; stale sweep will recover it",
            reservation_id,
            exc_info=True,
        )


@asynccontextmanager
async def _bounded_import_work(
    db: AsyncSession, reservation_id: str, user_id: str
):
    """Run all post-reservation work inside one hard, cleanup-safe deadline."""
    try:
        async with asyncio.timeout(import_work_timeout_seconds()):
            yield
    except TimeoutError as exc:
        # The timeout context has already converted its cancellation into a
        # normal exception, so cleanup runs outside the expired deadline.
        await _release_import_slug_after_failure(db, reservation_id, user_id)
        raise HTTPException(
            status_code=504,
            detail="Resident import timed out; pending creation was rolled back",
        ) from exc
    except BaseException:
        await _release_import_slug_after_failure(db, reservation_id, user_id)
        raise


async def _read_upload_limited(file: UploadFile) -> bytes:
    """Read at most one bounded upload, independent of client headers."""
    reported_size = getattr(file, "size", None)
    if reported_size is not None and reported_size > IMPORT_MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Import file exceeds the upload size limit")
    content = await file.read(IMPORT_MAX_UPLOAD_BYTES + 1)
    if len(content) > IMPORT_MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Import file exceeds the upload size limit")
    return content


@router.get("/{slug}/card")
async def resident_card(slug: str, db: AsyncSession = Depends(get_db)):
    """Public shareable card summary."""
    from fastapi import HTTPException
    from sqlalchemy import select as _select
    from app.models.memory import Memory
    resident = await get_resident_by_slug(db, slug)
    if not resident:
        raise HTTPException(status_code=404, detail="Resident not found")
    sbti = (resident.meta_json or {}).get("sbti", {})
    soul_first = (resident.soul_md or "").strip().split("\n\n")[0][:200]
    top = (await db.execute(
        _select(Memory.content).where(
            Memory.resident_id == resident.id, Memory.source == "reflection",
        ).order_by(Memory.importance.desc()).limit(1)
    )).scalar_one_or_none()
    return {
        "slug": resident.slug, "name": resident.name,
        "soul_excerpt": soul_first,
        "sbti_type": sbti.get("type"), "sbti_name": sbti.get("type_name"),
        "star_rating": resident.star_rating, "portrait_url": resident.portrait_url,
        "total_conversations": resident.total_conversations,
        "signature_reflection": top,
    }


@router.get("/{slug}/export")
async def resident_export(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Full export — owner only."""
    from fastapi import HTTPException
    user = await _require_user_auth(request, db)
    resident = await get_resident_by_slug(db, slug)
    if not resident:
        raise HTTPException(status_code=404, detail="Resident not found")
    if resident.creator_id != user.id:
        raise HTTPException(status_code=403, detail="Not your resident")
    return {
        "schema_version": CARD_SCHEMA_VERSION,
        "name": resident.name,
        "ability_md": resident.ability_md,
        "persona_md": resident.persona_md,
        "soul_md": resident.soul_md,
        "sbti": (resident.meta_json or {}).get("sbti"),
    }


class ImportBody(BaseModel):
    schema_version: int = CARD_SCHEMA_VERSION
    name: str
    ability_md: str = ""
    persona_md: str = ""
    soul_md: str = ""
    sbti: dict | None = None


# C1 soul-card JSON import. Path is /import-card, NOT /import: a second
# handler on /import shadowed the legacy multipart skill import (Starlette
# first-match wins) and broke the frontend importSkill in prod.
@router.post("/import-card")
async def resident_import(body: ImportBody, request: Request, db: AsyncSession = Depends(get_db)):
    from fastapi import HTTPException
    from app.services.content_guard import assert_resident_content_clean
    import random as _random

    user = await _require_user_auth(request, db)
    if not body.name.strip() or not (body.ability_md or body.persona_md or body.soul_md):
        raise HTTPException(status_code=400, detail="name and at least one layer are required")
    # Lightweight anti-abuse (full LLM validation_stage deferred to vm212).
    # 走统一守卫而不是逐字段手写——手写的那版漏了 ability_md 整整一轮。
    assert_resident_content_clean(
        name=body.name, ability_md=body.ability_md,
        persona_md=body.persona_md, soul_md=body.soul_md)

    requested_slug = _generate_slug(body.name)
    reservation = await _reserve_import_slug(
        db,
        user_id=user.id,
        name=body.name.strip(),
        slug=requested_slug,
        owner_kind="import_card",
        allow_suffix=True,
    )
    reservation_id = reservation.id
    reserved_slug = str(reservation.target_slug)

    async with _bounded_import_work(db, reservation_id, user.id):
        # Quota remains uncommitted until the Resident insert succeeds.
        await _claim_import_slot(db, user.id)
        district, tx, ty, home = await allocate_resident_location(
            db, ability_text=body.ability_md, persona_text=body.persona_md,
            soul_text=body.soul_md,
        )
        meta = {"origin": "import"}
        if body.sbti:
            meta["sbti"] = body.sbti
        # UGC type: an imported card is a player-authored character (creator_id
        # + origin="import"), not the player's avatar.
        resident = Resident(
            slug=reserved_slug, name=body.name.strip(), district=district,
            status="idle", heat=0, creator_id=user.id,
            resident_type=UGC_RESIDENT_TYPE,
            ability_md=body.ability_md, persona_md=body.persona_md,
            soul_md=body.soul_md, meta_json=meta,
            sprite_key=_random.choice(SPRITE_KEYS), tile_x=tx, tile_y=ty,
            home_location_id=home,
        )
        db.add(resident)
        await db.flush()
        consumed_slug = await consume_slug_reservation(
            db, reservation_id, user_id=user.id
        )
        if consumed_slug != reserved_slug:
            raise SlugReservationConflict("Slug reservation changed during import")
        # Resident insert, quota claim, and reservation release are atomic.
        await db.commit()
    await db.refresh(resident)
    return {"slug": resident.slug, "name": resident.name}


@router.post("/import", response_model=ResidentImportResponse)
@limiter.limit(lambda: f"{settings.rest_rate_limit_import_per_minute}/minute")
async def import_resident(
    request: Request,
    file: UploadFile = File(...),
    name: str = Form(...),
    slug: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    """Import a resident from SKILL.md or zip file."""
    user = await _require_user_auth(request, db)

    try:
        name, slug = validate_import_identity(name, slug)
    except SkillImportValidationError as exc:
        _raise_import_validation(exc)

    # Check slug uniqueness
    existing = await get_resident_by_slug(db, slug)
    if existing:
        raise HTTPException(status_code=409, detail="Slug already exists")

    content = await _read_upload_limited(file)
    filename = (file.filename or "").lower()

    # Parse based on file type
    layers: dict[str, str] | None = None
    combined_text: str | None = None
    meta_json: dict = {}

    try:
        if filename.endswith(".md") or filename.endswith(".txt"):
            combined_text = content.decode("utf-8", errors="replace")
            if not combined_text.strip():
                raise SkillImportValidationError("Imported Skill file is empty")
        elif filename.endswith(".zip"):
            layers, combined_text, meta_json = parse_skill_zip(content)
        else:
            raise SkillImportValidationError("Unsupported file format. Use .md, .txt, or .zip")
    except SkillImportValidationError as exc:
        _raise_import_validation(exc)

    detected_format: SkillFormat | None = None
    if combined_text is not None:
        detected_format = detect_skill_format(combined_text)
        if detected_format == SkillFormat.STANDARD_3LAYER:
            # This branch is a deterministic parser, not an LLM call. Parse and
            # size-check it before reserving quota or consulting cost gates.
            try:
                layers = validate_import_layers(
                    await convert_to_standard(combined_text, detected_format)
                )
            except SkillImportValidationError as exc:
                _raise_import_validation(exc)
            combined_text = None

    # Standard layer files still run SBTI when they contain enough text;
    # non-standard combined files always require conversion. Gate before any
    # user-triggered LLM request.
    needs_conversion = combined_text is not None
    candidate_length = (
        len(combined_text or "")
        if layers is None
        else sum(len(value) for value in layers.values())
    )
    if (needs_conversion or candidate_length >= 50) and await forge_blocked(db, user.id):
        raise HTTPException(status_code=402, detail="Daily LLM budget reached — try again later")

    reservation = await _reserve_import_slug(
        db,
        user_id=user.id,
        name=name,
        slug=slug,
        owner_kind="skill_import",
        allow_suffix=False,
    )
    reservation_id = reservation.id
    reserved_slug = str(reservation.target_slug)

    async with _bounded_import_work(db, reservation_id, user.id):
        # Atomic with the Resident insert below. Keeping the quota claim ahead
        # of LLM calls prevents concurrent requests from bypassing admission,
        # while failure still rolls the claim back.
        await _claim_import_slot(db, user.id)

        if combined_text is not None:
            try:
                layers = await convert_to_standard(
                    combined_text,
                    detected_format or SkillFormat.PLAIN_TEXT,
                    user_id=user.id,
                )
            except SkillImportValidationError as exc:
                _raise_import_validation(exc)
            except Exception as exc:
                raise HTTPException(
                    status_code=502, detail="Unable to convert imported Skill"
                ) from exc

        try:
            layers = validate_import_layers(layers or {})
        except SkillImportValidationError as exc:
            _raise_import_validation(exc)
        ability_md = layers["ability_md"]
        persona_md = layers["persona_md"]
        soul_md = layers["soul_md"]

        from app.services.content_guard import assert_resident_content_clean
        assert_resident_content_clean(
            name=name, ability_md=ability_md,
            persona_md=persona_md, soul_md=soul_md)

        # Create a mock-like object for scoring
        class _ResidentForScoring:
            pass
        r_score = _ResidentForScoring()
        r_score.ability_md = ability_md
        r_score.persona_md = persona_md
        r_score.soul_md = soul_md
        r_score.total_conversations = 0
        r_score.avg_rating = 0.0

        star_rating = compute_star_rating(r_score)

        district, tile_x, tile_y, home_loc_id = await allocate_resident_location(
            db,
            ability_text=ability_md,
            persona_text=persona_md,
            soul_text=soul_md,
        )

        # Compute SBTI personality (non-blocking: skip if fails). Conversion is
        # separately metered, so consult the breaker again after it.
        final_meta = {**meta_json, "origin": "import"}
        sbti = None
        sbti_input_length = len(ability_md) + len(persona_md) + len(soul_md) + 2
        if sbti_input_length >= 50 and not await forge_blocked(db, user.id):
            sbti = await compute_sbti(
                name, ability_md, persona_md, soul_md, user_id=user.id
            )
        if sbti:
            final_meta = update_meta_with_sbti(final_meta, sbti)

        # UGC type: same creation act as import-card, just a file upload.
        resident = Resident(
            slug=reserved_slug,
            name=name,
            district=district,
            status="idle",
            heat=0,
            model_tier="standard",
            token_cost_per_turn=1,
            creator_id=user.id,
            resident_type=UGC_RESIDENT_TYPE,
            ability_md=ability_md,
            persona_md=persona_md,
            soul_md=soul_md,
            meta_json=final_meta,
            sprite_key=random.choice(SPRITE_KEYS),
            tile_x=tile_x,
            tile_y=tile_y,
            star_rating=star_rating,
            home_location_id=home_loc_id,
        )
        db.add(resident)
        await db.flush()
        consumed_slug = await consume_slug_reservation(
            db, reservation_id, user_id=user.id
        )
        if consumed_slug != reserved_slug:
            raise SlugReservationConflict("Slug reservation changed during import")
        # Resident, quota, and reservation release become visible atomically.
        await db.commit()
    await db.refresh(resident)

    return ResidentImportResponse(
        id=resident.id,
        slug=resident.slug,
        name=resident.name,
        district=resident.district,
        star_rating=resident.star_rating,
        ability_md=resident.ability_md,
        persona_md=resident.persona_md,
        soul_md=resident.soul_md,
        meta_json=resident.meta_json,
    )


@router.put("/player/position")
async def update_player_position(
    request: Request,
    req: PlayerPositionUpdate,
    db: AsyncSession = Depends(get_db),
):
    """Persist the player's tile coordinates (from teleport) to the user record."""
    user = await _require_user_auth(request, db)
    if not user.player_resident_id:
        raise HTTPException(status_code=404, detail="No player resident")

    result = await db.execute(select(User).where(User.id == user.id))
    db_user = result.scalar_one_or_none()
    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    TILE_SIZE = 32
    db_user.last_x = req.tile_x * TILE_SIZE + TILE_SIZE // 2
    db_user.last_y = req.tile_y * TILE_SIZE + TILE_SIZE
    await db.commit()
    return {"tile_x": req.tile_x, "tile_y": req.tile_y}


@router.get("/{slug}", response_model=ResidentDetail)
async def get_one(slug: str, db: AsyncSession = Depends(get_db)):
    r = await get_resident_by_slug(db, slug)
    if not r:
        raise HTTPException(status_code=404, detail="Resident not found")
    return ResidentDetail.model_validate(r, from_attributes=True)


@router.put("/{slug}", response_model=ResidentDetail)
@limiter.limit(lambda: f"{settings.rest_rate_limit_resident_edit_per_minute}/minute")
async def edit_resident(
    slug: str,
    req: ResidentEditRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Edit a resident's three layers. Creator only. Auto-versions before each edit."""
    user = await _require_user_auth(request, db)

    r = await get_resident_by_slug(db, slug)
    if not r:
        raise HTTPException(status_code=404, detail="Resident not found")
    if r.creator_id != user.id:
        raise HTTPException(status_code=403, detail="Only the creator can edit this resident")

    # 内容守卫必须先于快照——create_version_snapshot 会 commit，先快照再拒绝
    # 等于给一次被拒的编辑留下一个版本行。
    from app.services.content_guard import assert_resident_content_clean
    assert_resident_content_clean(
        ability_md=req.ability_md, persona_md=req.persona_md,
        soul_md=req.soul_md)

    # Snapshot current state before editing
    await create_version_snapshot(db, r)

    # Re-fetch (version service commits, so r may be stale)
    r = await get_resident_by_slug(db, slug)

    # Apply updates (only non-None fields)
    if req.ability_md is not None:
        r.ability_md = req.ability_md
    if req.persona_md is not None:
        r.persona_md = req.persona_md
    if req.soul_md is not None:
        r.soul_md = req.soul_md

    # Recalculate star rating
    r.star_rating = compute_star_rating(r)

    # Recalculate SBTI when personality layers change
    if req.ability_md is not None or req.persona_md is not None or req.soul_md is not None:
        # Editing remains available after the user's LLM budget is exhausted;
        # only the derived, paid SBTI refresh is skipped.
        if not await forge_blocked(db, user.id):
            sbti = await compute_sbti(
                r.name, r.ability_md, r.persona_md, r.soul_md, user_id=user.id
            )
            if sbti:
                r.meta_json = update_meta_with_sbti(r.meta_json, sbti)

    await db.commit()
    await db.refresh(r)
    return ResidentDetail.model_validate(r, from_attributes=True)


@router.get("/{slug}/versions", response_model=list[VersionSnapshot])
async def get_resident_versions(
    slug: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Get version history for a resident."""
    user = await _require_user_auth(request, db)
    r = await get_resident_by_slug(db, slug)
    if not r:
        raise HTTPException(status_code=404, detail="Resident not found")
    if r.creator_id != user.id:
        raise HTTPException(status_code=403, detail="Only the creator can view versions")
    return await get_versions(db, r.id)
