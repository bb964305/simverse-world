"""State machine and atomic publication for reviewed resident sprites."""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import tempfile
import fcntl
from datetime import UTC, datetime
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.resident import Resident
from app.models.resident_sprite_run import ResidentSpriteRun
from app.schemas.resident_sprite import CHECKLIST_KEYS
from app.services.resident_sprite_generation import ResidentSpriteRequest, new_run_id
from app.services.resident_sprite_qc import inspect_resident_sprite_atlas


class SpriteWorkflowError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _error(status: int, code: str, message: str) -> SpriteWorkflowError:
    return SpriteWorkflowError(status, code, message)


def confined_artifact_path(raw_path: str, *, must_exist: bool = False) -> Path:
    """Resolve a worker artifact without allowing traversal or symlink escape."""
    root = Path(settings.resident_sprite_artifact_dir).resolve()
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=must_exist)
    except (FileNotFoundError, OSError) as exc:
        raise _error(422, "CANDIDATE_NOT_FOUND", "Candidate artifact does not exist") from exc
    if not resolved.is_relative_to(root):
        raise _error(422, "PATH_OUTSIDE_ARTIFACT_ROOT", "Artifact path is outside the configured root")
    if must_exist and (not resolved.is_file() or resolved.is_symlink()):
        raise _error(422, "INVALID_CANDIDATE_PATH", "Candidate artifact must be a regular file")
    return resolved


def normalize_artifact_path(raw_path: str | None) -> str | None:
    return str(confined_artifact_path(raw_path)) if raw_path else None


def _read_png(path: Path, *, texture: bool) -> tuple[bytes, str]:
    try:
        data = path.read_bytes()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.format != "PNG" or image.mode != "RGBA":
                raise _error(422, "INVALID_PNG", "Candidate must be an RGBA PNG")
            if texture and image.size != (96, 128):
                raise _error(422, "INVALID_TEXTURE_SIZE", "Texture must be exactly 96x128 pixels")
            if not texture and (image.width < 1 or image.height < 1):
                raise _error(422, "INVALID_PORTRAIT_SIZE", "Portrait dimensions must be positive")
    except SpriteWorkflowError:
        raise
    except (OSError, UnidentifiedImageError) as exc:
        raise _error(422, "INVALID_PNG", "Candidate is not a valid PNG") from exc
    return data, hashlib.sha256(data).hexdigest()


def _verify_declared_hash(actual: str, declared: str | None, label: str) -> None:
    if declared and declared != actual:
        raise _error(409, "CONTENT_HASH_MISMATCH", f"{label} SHA-256 does not match candidate")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _publish_directory(parent: Path, digest: str, texture: bytes, portrait: bytes) -> Path:
    """Make both files visible at once; never mutate an existing digest directory."""
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / digest
    lock_path = parent / ".publish.lock"
    try:
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if destination.exists():
                expected = {"texture.png": texture, "portrait.png": portrait}
                if not destination.is_dir() or any(
                    not (destination / name).is_file()
                    or hashlib.sha256((destination / name).read_bytes()).digest()
                    != hashlib.sha256(data).digest()
                    for name, data in expected.items()
                ):
                    raise _error(409, "IMMUTABLE_PATH_CONFLICT", "Published content directory already differs")
                return destination

            staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=parent))
            try:
                _atomic_write(staging / "texture.png", texture)
                _atomic_write(staging / "portrait.png", portrait)
                _fsync_directory(staging)
                os.rename(staging, destination)
                _fsync_directory(parent)
                return destination
            finally:
                shutil.rmtree(staging, ignore_errors=True)
    except SpriteWorkflowError:
        raise
    except OSError as exc:
        raise _error(500, "PUBLICATION_IO_FAILED", "Could not persist the published sprite") from exc


async def get_run(db: AsyncSession, run_id: str) -> ResidentSpriteRun:
    run = (await db.execute(
        select(ResidentSpriteRun).where(ResidentSpriteRun.run_id == run_id)
    )).scalar_one_or_none()
    if run is None:
        raise _error(404, "RUN_NOT_FOUND", "Resident sprite run not found")
    return run


async def create_run(db: AsyncSession, **values) -> ResidentSpriteRun:
    resident_id = values.pop("resident_id")
    resident = await db.get(Resident, resident_id)
    if resident is None:
        raise _error(404, "RESIDENT_NOT_FOUND", "Resident not found")
    request_values = {
        "asset_key": _resident_asset_key(resident),
        "display_name": resident.name,
        "appearance": values.pop("appearance") or _default_appearance(resident),
        "gender": values.pop("gender"),
        "age_group": values.pop("age_group"),
        "vibe": values.pop("vibe") or _default_vibe(resident),
        "tags": values.pop("tags") or _default_tags(resident),
        "direction_policy": values.pop("direction_policy"),
        "model": settings.resident_sprite_provider_model,
    }
    try:
        generation_request = ResidentSpriteRequest.model_validate(request_values)
    except Exception as exc:
        raise _error(422, "GENERATION_REQUEST_INVALID", "Sprite generation request is invalid") from exc
    run = ResidentSpriteRun(
        resident_id=resident_id,
        run_id=new_run_id(),
        status="requested",
        direction_policy=generation_request.direction_policy,
        generation_request_json=generation_request.model_dump(mode="json"),
        attempts=0,
        request_count=0,
        estimated_cost_usd=None,
    )
    active_run_id = run.run_id
    db.add(run)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        if await db.scalar(select(ResidentSpriteRun.id).where(ResidentSpriteRun.run_id == active_run_id)):
            raise _error(409, "RUN_ID_EXISTS", "run_id already exists")
        raise
    await db.refresh(run)
    return run


def _resident_asset_key(resident: Resident) -> str:
    normalized = "".join(
        character if character.isascii() and (character.isalnum() or character in "_-") else "-"
        for character in resident.slug.lower()
    ).strip("-_")
    normalized = "-".join(part for part in normalized.split("-") if part)[:64]
    return normalized or f"resident-{resident.id.replace('-', '')[:16]}"


def _default_appearance(resident: Resident) -> str:
    source = " ".join(
        part.strip() for part in (resident.persona_md or "", resident.ability_md or "") if part.strip()
    )
    return (source[:1200] or f"A distinctive resident of the {resident.district} district")


def _default_vibe(resident: Resident) -> str:
    district = (resident.district or "distinctive").strip()
    return district[:40] or "distinctive"


def _default_tags(resident: Resident) -> list[str]:
    values = [resident.district or "free", resident.resident_type or "npc"]
    return list(dict.fromkeys(value.strip()[:32] for value in values if value.strip()))


async def list_runs(
    db: AsyncSession, *, page: int, per_page: int, resident_id: str | None, status: str | None
) -> tuple[list[ResidentSpriteRun], int]:
    filters = []
    if resident_id:
        filters.append(ResidentSpriteRun.resident_id == resident_id)
    if status:
        filters.append(ResidentSpriteRun.status == status)
    total = await db.scalar(select(func.count(ResidentSpriteRun.id)).where(*filters)) or 0
    rows = await db.execute(
        select(ResidentSpriteRun).where(*filters).order_by(ResidentSpriteRun.created_at.desc())
        .offset((page - 1) * per_page).limit(per_page)
    )
    return list(rows.scalars()), total


async def _conditional_update(
    db: AsyncSession,
    run: ResidentSpriteRun,
    expected_version: int,
    allowed_states: set[str],
    values: dict,
) -> ResidentSpriteRun:
    if run.version != expected_version:
        raise _error(409, "VERSION_CONFLICT", "Run was modified; reload before retrying")
    if run.status not in allowed_states:
        raise _error(409, "INVALID_STATE", f"Action is not allowed while run is {run.status}")
    values.update(version=expected_version + 1, updated_at=datetime.now(UTC))
    result = await db.execute(
        update(ResidentSpriteRun).where(
            ResidentSpriteRun.id == run.id,
            ResidentSpriteRun.version == expected_version,
            ResidentSpriteRun.status.in_(allowed_states),
        ).values(**values)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise _error(409, "VERSION_CONFLICT", "Run was modified; reload before retrying")
    await db.commit()
    return await get_run(db, run.run_id)


async def update_progress(db: AsyncSession, run: ResidentSpriteRun, body) -> ResidentSpriteRun:
    if body.action == "retry":
        if run.status in {"quarantined", "rejected"}:
            if run.version != body.expected_version:
                raise _error(409, "VERSION_CONFLICT", "Run was modified; reload before retrying")
            result = await db.execute(
                update(ResidentSpriteRun).where(
                    ResidentSpriteRun.id == run.id,
                    ResidentSpriteRun.version == body.expected_version,
                    ResidentSpriteRun.status.in_(("quarantined", "rejected")),
                ).values(
                    status="retry_spawned", version=body.expected_version + 1,
                    updated_at=datetime.now(UTC),
                )
            )
            if result.rowcount != 1:
                await db.rollback()
                raise _error(409, "VERSION_CONFLICT", "Run was modified; reload before retrying")
            replacement = ResidentSpriteRun(
                resident_id=run.resident_id,
                run_id=new_run_id(),
                status="retrying",
                direction_policy=run.direction_policy,
                generation_request_json=dict(run.generation_request_json),
                retry_of_run_id=run.run_id,
                attempts=0,
                request_count=0,
                estimated_cost_usd=None,
            )
            db.add(replacement)
            await db.commit()
            await db.refresh(replacement)
            return replacement
        return await _conditional_update(db, run, body.expected_version, {"failed"}, {
            "status": "retrying", "error_code": None, "error_message": None,
            "lease_owner": None, "lease_expires_at": None,
        })

    if run.status in {"requested", "interrupted"}:
        return await _conditional_update(db, run, body.expected_version, {"requested", "interrupted"}, {
            "status": "requested", "lease_owner": None, "lease_expires_at": None,
            "error_code": None, "error_message": None,
        })
    if run.status != "generating":
        raise _error(409, "INVALID_STATE", f"Action is not allowed while run is {run.status}")
    if run.version != body.expected_version:
        raise _error(409, "VERSION_CONFLICT", "Run was modified; reload before retrying")
    active_run_id = run.run_id
    now = datetime.now(UTC)
    result = await db.execute(
        update(ResidentSpriteRun).where(
            ResidentSpriteRun.id == run.id,
            ResidentSpriteRun.version == body.expected_version,
            ResidentSpriteRun.status == "generating",
            ResidentSpriteRun.lease_expires_at.is_not(None),
            ResidentSpriteRun.lease_expires_at <= func.now(),
        ).values(
            status="requested", lease_owner=None, lease_expires_at=None,
            error_code=None, error_message=None, version=body.expected_version + 1,
            updated_at=now,
        ).execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise _error(409, "LEASE_ACTIVE", "The generation lease is still active")
    await db.commit()
    db.expire_all()
    return await get_run(db, active_run_id)


async def submit_review(db: AsyncSession, run: ResidentSpriteRun, body, admin_id: str) -> ResidentSpriteRun:
    if not run.candidate_texture_path or not run.candidate_portrait_path:
        raise _error(409, "CANDIDATE_REQUIRED", "A complete candidate is required for review")
    return await _conditional_update(db, run, body.expected_version, {"candidate_ready", "in_review"}, {
        "status": "in_review", "review_evidence_json": body.evidence,
        "review_checklist_json": body.checklist, "review_notes": body.notes,
        "reviewed_by": admin_id, "reviewed_at": datetime.now(UTC),
    })


async def approve(db: AsyncSession, run: ResidentSpriteRun, expected_version: int, admin_id: str) -> ResidentSpriteRun:
    checklist = run.review_checklist_json or {}
    if set(checklist) != set(CHECKLIST_KEYS) or not all(checklist.values()):
        raise _error(409, "CHECKLIST_INCOMPLETE", "All nine review checks must pass before approval")
    return await _conditional_update(db, run, expected_version, {"in_review"}, {
        "status": "approved", "reviewed_by": admin_id, "reviewed_at": datetime.now(UTC),
        "rejection_reason": None,
    })


async def reject(
    db: AsyncSession, run: ResidentSpriteRun, expected_version: int, admin_id: str, reason: str
) -> ResidentSpriteRun:
    return await _conditional_update(db, run, expected_version, {"candidate_ready", "in_review", "approved"}, {
        "status": "rejected", "reviewed_by": admin_id, "reviewed_at": datetime.now(UTC),
        "rejection_reason": reason,
    })


async def publish(
    db: AsyncSession, run: ResidentSpriteRun, expected_version: int, admin_id: str
) -> tuple[ResidentSpriteRun, Resident]:
    if run.version != expected_version:
        raise _error(409, "VERSION_CONFLICT", "Run was modified; reload before retrying")
    if run.status != "approved":
        raise _error(409, "NOT_APPROVED", "Only an approved candidate can be published")
    if not run.candidate_texture_path or not run.candidate_portrait_path:
        raise _error(409, "CANDIDATE_REQUIRED", "A complete candidate is required")

    texture_path = confined_artifact_path(run.candidate_texture_path, must_exist=True)
    portrait_path = confined_artifact_path(run.candidate_portrait_path, must_exist=True)
    texture, texture_sha = _read_png(texture_path, texture=True)
    portrait, portrait_sha = _read_png(portrait_path, texture=False)
    _verify_declared_hash(texture_sha, run.candidate_texture_sha256, "Texture")
    _verify_declared_hash(portrait_sha, run.candidate_portrait_sha256, "Portrait")
    findings = inspect_resident_sprite_atlas(texture, direction_policy=run.direction_policy)
    if findings:
        codes = ", ".join(finding.code for finding in findings)
        raise _error(422, "AUTOMATED_QC_FAILED", f"Atlas QC failed: {codes}")

    resident = (await db.execute(
        select(Resident).where(Resident.id == run.resident_id).with_for_update()
    )).scalar_one_or_none()
    if resident is None:
        raise _error(404, "RESIDENT_NOT_FOUND", "Resident not found")
    publication_digest = hashlib.sha256(f"{texture_sha}:{portrait_sha}".encode("ascii")).hexdigest()
    relative = Path("resident-sprites") / resident.id / publication_digest
    _publish_directory(Path(settings.static_dir).resolve() / "resident-sprites" / resident.id,
                       publication_digest, texture, portrait)
    sprite_url = f"/static/{relative.as_posix()}/texture.png"
    portrait_url = f"/static/{relative.as_posix()}/portrait.png"

    result = await db.execute(
        update(ResidentSpriteRun).where(
            ResidentSpriteRun.id == run.id,
            ResidentSpriteRun.version == expected_version,
            ResidentSpriteRun.status == "approved",
        ).values(
            status="published", version=expected_version + 1,
            published_texture_sha256=texture_sha, published_portrait_sha256=portrait_sha,
            published_by=admin_id, published_at=datetime.now(UTC), updated_at=datetime.now(UTC),
            previous_sprite_url=resident.sprite_url, previous_portrait_url=resident.portrait_url,
            previous_sprite_content_hash=resident.sprite_content_hash,
            previous_sprite_generation_run_id=resident.sprite_generation_run_id,
        )
    )
    if result.rowcount != 1:
        await db.rollback()
        raise _error(409, "VERSION_CONFLICT", "Run was modified; reload before retrying")
    if resident.sprite_generation_run_id and resident.sprite_generation_run_id != run.run_id:
        await db.execute(
            update(ResidentSpriteRun).where(
                ResidentSpriteRun.run_id == resident.sprite_generation_run_id,
                ResidentSpriteRun.status == "published",
            ).values(
                status="superseded", version=ResidentSpriteRun.version + 1,
                updated_at=datetime.now(UTC),
            )
        )
    resident.sprite_url = sprite_url
    resident.portrait_url = portrait_url
    resident.sprite_content_hash = texture_sha
    resident.sprite_generation_run_id = run.run_id
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    await db.refresh(resident)
    return await get_run(db, run.run_id), resident


async def rollback(
    db: AsyncSession, run: ResidentSpriteRun, expected_version: int, admin_id: str, reason: str
) -> tuple[ResidentSpriteRun, Resident]:
    if run.version != expected_version:
        raise _error(409, "VERSION_CONFLICT", "Run was modified; reload before retrying")
    if run.status != "published":
        raise _error(409, "INVALID_STATE", "Only a published run can be rolled back")
    resident = await db.get(Resident, run.resident_id)
    if resident is None:
        raise _error(404, "RESIDENT_NOT_FOUND", "Resident not found")
    if resident.sprite_generation_run_id != run.run_id:
        raise _error(409, "NOT_CURRENT_PUBLICATION", "A newer sprite is already active")

    result = await db.execute(
        update(ResidentSpriteRun).where(
            ResidentSpriteRun.id == run.id,
            ResidentSpriteRun.version == expected_version,
            ResidentSpriteRun.status == "published",
        ).values(
            status="rolled_back", version=expected_version + 1, rolled_back_by=admin_id,
            rolled_back_at=datetime.now(UTC), rollback_reason=reason, updated_at=datetime.now(UTC),
        )
    )
    if result.rowcount != 1:
        await db.rollback()
        raise _error(409, "VERSION_CONFLICT", "Run was modified; reload before retrying")
    if run.previous_sprite_generation_run_id:
        await db.execute(
            update(ResidentSpriteRun).where(
                ResidentSpriteRun.run_id == run.previous_sprite_generation_run_id,
                ResidentSpriteRun.status == "superseded",
            ).values(
                status="published", version=ResidentSpriteRun.version + 1,
                updated_at=datetime.now(UTC),
            )
        )
    resident.sprite_url = run.previous_sprite_url
    resident.portrait_url = run.previous_portrait_url
    resident.sprite_content_hash = run.previous_sprite_content_hash
    resident.sprite_generation_run_id = run.previous_sprite_generation_run_id
    await db.commit()
    await db.refresh(resident)
    return await get_run(db, run.run_id), resident
