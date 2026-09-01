"""Onboarding router: resident creation and verified Passport binding."""
from datetime import UTC, datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.user import User
from app.models.resident import Resident
from app.models.web3_agent_passport import Web3AgentPassport
from app.services.auth_service import get_current_user
from app.services.onboarding_service import (
    check_onboarding_needed,
    create_player_resident,
    load_preset_as_player,
    skip_onboarding,
)
from app.services.web3_registry_service import (
    PassportVerificationError,
    verify_passport_registration,
)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


async def _require_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing token")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user


# --- Request / Response schemas ---

class CreateCharacterRequest(BaseModel):
    name: str
    sprite_key: str
    reply_mode: str = "auto"
    ability_md: str = ""
    persona_md: str = ""
    soul_md: str = ""
    portrait_url: str | None = None


class LoadPresetRequest(BaseModel):
    preset_slug: str


class ResidentResponse(BaseModel):
    id: str
    slug: str
    name: str
    sprite_key: str
    tile_x: int
    tile_y: int

    class Config:
        from_attributes = True


class ConfirmPassportRequest(BaseModel):
    resident_id: str
    agent_id: str
    transaction_hash: str | None = None
    metadata_uri: str
    metadata_hash: str


class PassportResponse(BaseModel):
    resident_id: str
    agent_id: str
    chain_id: int
    registry_address: str
    transaction_hash: str | None
    metadata_uri: str
    metadata_hash: str


# --- Endpoints ---

@router.get("/check")
async def check(request: Request, db: AsyncSession = Depends(get_db)):
    """Check if the current user needs onboarding."""
    user = await _require_user(request, db)
    return await check_onboarding_needed(db, user.id)


@router.post("/create-character", response_model=ResidentResponse)
async def create_character(
    body: CreateCharacterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Create a new player resident from scratch."""
    user = await _require_user(request, db)
    from app.services.content_guard import assert_resident_content_clean
    assert_resident_content_clean(
        name=body.name, ability_md=body.ability_md,
        persona_md=body.persona_md, soul_md=body.soul_md)
    try:
        resident = await create_player_resident(
            db=db,
            user_id=user.id,
            name=body.name,
            sprite_key=body.sprite_key,
            reply_mode=body.reply_mode,
            ability_md=body.ability_md,
            persona_md=body.persona_md,
            soul_md=body.soul_md,
            portrait_url=body.portrait_url,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ResidentResponse.model_validate(resident, from_attributes=True)


@router.post("/load-preset", response_model=ResidentResponse)
async def load_preset(
    body: LoadPresetRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Load a preset resident as the player character."""
    user = await _require_user(request, db)
    try:
        resident = await load_preset_as_player(db, user.id, body.preset_slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ResidentResponse.model_validate(resident, from_attributes=True)


@router.post("/skip", response_model=ResidentResponse)
async def skip(request: Request, db: AsyncSession = Depends(get_db)):
    """Skip onboarding and create a default player resident."""
    user = await _require_user(request, db)
    try:
        resident = await skip_onboarding(db, user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return ResidentResponse.model_validate(resident, from_attributes=True)


@router.post("/passport/confirm", response_model=PassportResponse)
async def confirm_passport(
    body: ConfirmPassportRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Persist a resident Passport only after verifying the live chain state."""
    user = await _require_user(request, db)
    resident = (await db.execute(
        select(Resident).where(
            Resident.id == body.resident_id,
            Resident.creator_id == user.id,
        )
    )).scalar_one_or_none()
    if resident is None or user.player_resident_id != resident.id:
        raise HTTPException(status_code=404, detail="Owned player resident not found")

    existing = (await db.execute(
        select(Web3AgentPassport).where(Web3AgentPassport.resident_id == resident.id)
    )).scalar_one_or_none()
    if existing is not None:
        if existing.user_id != user.id or existing.agent_id != body.agent_id:
            raise HTTPException(status_code=409, detail="Resident already has another Agent Passport")

    try:
        verified = await verify_passport_registration(
            wallet_address=user.wallet_address or "",
            resident_id=resident.id,
            agent_id=body.agent_id,
            transaction_hash=body.transaction_hash,
            metadata_uri=body.metadata_uri,
            metadata_hash=body.metadata_hash,
        )
    except PassportVerificationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    row = existing or Web3AgentPassport(user_id=user.id, resident_id=resident.id)
    row.chain_id = verified.chain_id
    row.registry_address = verified.registry_address
    row.agent_id = verified.agent_id
    row.resident_key = verified.resident_key
    if body.transaction_hash:
        row.registration_tx_hash = body.transaction_hash.lower()
    row.metadata_uri = body.metadata_uri[:1000]
    row.metadata_hash = body.metadata_hash.lower()
    row.updated_at = datetime.now(UTC)
    if existing is None:
        db.add(row)
    await db.commit()
    return PassportResponse(
        resident_id=row.resident_id,
        agent_id=row.agent_id,
        chain_id=row.chain_id,
        registry_address=row.registry_address,
        transaction_hash=row.registration_tx_hash,
        metadata_uri=row.metadata_uri,
        metadata_hash=row.metadata_hash,
    )
