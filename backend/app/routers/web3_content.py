"""Private off-chain content storage for on-chain Simverse anchors."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import desc, select

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.models.resident import Resident
from app.models.memory import Memory
from app.services.auth_service import get_current_user

router = APIRouter(prefix="/web3/content", tags=["web3-content"])


class ContentUploadResponse(BaseModel):
    content_id: str
    content_uri: str
    content_hash: str
    filename: str
    media_type: str
    size: int


async def _wallet_user(request: Request, db: AsyncSession) -> User:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing wallet session")
    user = await get_current_user(db, auth.removeprefix("Bearer "))
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid wallet session")
    if not user.wallet_address:
        raise HTTPException(status_code=403, detail="Wallet identity required")
    return user


def _user_root(user_id: str) -> Path:
    root = Path(settings.web3_content_dir).resolve()
    target = (root / user_id).resolve()
    if not target.is_relative_to(root):
        raise HTTPException(status_code=400, detail="Invalid storage identity")
    target.mkdir(parents=True, exist_ok=True)
    return target


def _public_content_uri(request: Request, content_id: str) -> str:
    configured = settings.web3_public_api_base_url.strip().rstrip("/")
    base_url = configured or str(request.base_url).rstrip("/")
    return f"{base_url}/web3/content/{content_id}"


def _store_snapshot(
    *, request: Request, user: User, payload: bytes, filename: str, media_type: str
) -> ContentUploadResponse:
    if not payload:
        raise HTTPException(status_code=400, detail="Empty content cannot be anchored")
    if len(payload) > settings.web3_content_max_bytes:
        raise HTTPException(status_code=413, detail="Web3 content exceeds upload limit")
    content_id = str(uuid.uuid4())
    user_root = _user_root(user.id)
    digest = hashlib.sha256(payload).hexdigest()
    (user_root / f"{content_id}.bin").write_bytes(payload)
    (user_root / f"{content_id}.json").write_text(json.dumps({
        "filename": filename,
        "media_type": media_type,
        "size": len(payload),
        "sha256": digest,
    }, ensure_ascii=False), encoding="utf-8")
    return ContentUploadResponse(
        content_id=content_id,
        content_uri=_public_content_uri(request, content_id),
        content_hash=f"0x{digest}",
        filename=filename,
        media_type=media_type,
        size=len(payload),
    )


@router.post("", response_model=ContentUploadResponse)
async def upload_content(
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Store wallet-owned content and return its SHA-256 chain anchor."""
    user = await _wallet_user(request, db)
    content_id = str(uuid.uuid4())
    user_root = _user_root(user.id)
    data_path = user_root / f"{content_id}.bin"
    meta_path = user_root / f"{content_id}.json"
    digest = hashlib.sha256()
    size = 0

    try:
        with data_path.open("xb") as output:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.web3_content_max_bytes:
                    raise HTTPException(status_code=413, detail="Web3 content exceeds upload limit")
                digest.update(chunk)
                output.write(chunk)
        if size == 0:
            raise HTTPException(status_code=400, detail="Empty content cannot be anchored")

        filename = Path(file.filename or "content.bin").name[:255]
        media_type = (file.content_type or "application/octet-stream")[:127]
        metadata = {
            "filename": filename,
            "media_type": media_type,
            "size": size,
            "sha256": digest.hexdigest(),
        }
        meta_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
    except Exception:
        data_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    content_uri = _public_content_uri(request, content_id)
    return ContentUploadResponse(
        content_id=content_id,
        content_uri=content_uri,
        content_hash=f"0x{digest.hexdigest()}",
        filename=filename,
        media_type=media_type,
        size=size,
    )


@router.post("/memory-snapshot/{resident_id}", response_model=ContentUploadResponse)
async def create_memory_snapshot(
    resident_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Export the wallet owner's current in-game memories as an anchorable snapshot."""
    user = await _wallet_user(request, db)
    resident = (await db.execute(
        select(Resident).where(Resident.id == resident_id, Resident.creator_id == user.id)
    )).scalar_one_or_none()
    if resident is None:
        raise HTTPException(status_code=404, detail="Owned resident not found")
    memories = (await db.execute(
        select(Memory)
        .where(Memory.resident_id == resident.id)
        .order_by(desc(Memory.created_at), desc(Memory.id))
        .limit(2000)
    )).scalars().all()
    snapshot = {
        "schema": "simverse-memory-v1",
        "resident": {"id": resident.id, "slug": resident.slug, "name": resident.name},
        "owner": user.wallet_address,
        "memory_count": len(memories),
        "memories": [{
            "id": memory.id,
            "type": memory.type,
            "content": memory.content,
            "importance": memory.importance,
            "source": memory.source,
            "related_resident_id": memory.related_resident_id,
            "related_user_id": memory.related_user_id,
            "media_url": memory.media_url,
            "media_summary": memory.media_summary,
            "metadata": memory.metadata_json,
            "created_at": memory.created_at.isoformat() if memory.created_at else None,
            "archived_at": memory.archived_at.isoformat() if memory.archived_at else None,
        } for memory in reversed(memories)],
    }
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _store_snapshot(
        request=request,
        user=user,
        payload=payload,
        filename=f"{resident.slug}-memory-snapshot.json",
        media_type="application/json",
    )


@router.get("/{content_id}")
async def download_content(
    content_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Download content only when the same wallet identity owns it."""
    user = await _wallet_user(request, db)
    user_root = _user_root(user.id)
    identifier = str(content_id)
    data_path = user_root / f"{identifier}.bin"
    meta_path = user_root / f"{identifier}.json"
    if not data_path.is_file() or not meta_path.is_file():
        raise HTTPException(status_code=404, detail="Anchored content not found")
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail="Anchored content metadata is invalid") from exc
    return FileResponse(
        data_path,
        filename=str(metadata.get("filename") or "content.bin"),
        media_type=str(metadata.get("media_type") or "application/octet-stream"),
    )
