"""Lease-based consumer for durable resident sprite generation requests."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Awaitable, Callable

from pydantic import ValidationError
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.database import async_session
from app.http import get_client
from app.models.resident_sprite_run import ResidentSpriteRun
from app.services.resident_sprite_artifacts import load_run
from app.services.resident_sprite_generation import (
    CapabilityContract,
    CapabilityReceipt,
    QualifiedSpriteCapability,
    ResidentSpriteContractError,
    ResidentSpriteRequest,
    ResidentSpriteRunResult,
    generate_resident_sprite,
    validate_capability_receipt,
    validate_non_symlink_path,
)
from app.services.resident_sprite_provider import ProviderConfig, ResidentSpriteProvider

logger = logging.getLogger(__name__)
_COST_QUANTUM_USD = Decimal("0.000001")


def estimate_cost_upper_bound(request_count: int, per_request_usd: float) -> float | None:
    if per_request_usd <= 0:
        return None
    total = Decimal(request_count) * Decimal(str(per_request_usd))
    return float(total.quantize(_COST_QUANTUM_USD, rounding=ROUND_CEILING))


@dataclass(frozen=True)
class ClaimedSpriteRun:
    id: str
    run_id: str
    request: ResidentSpriteRequest
    owner: str
    attempts: int
    previous_status: str


class SpriteWorkerError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def worker_owner() -> str:
    return f"{socket.gethostname()}:{uuid.uuid4().hex}"


def validate_provider_binding(
    config: ProviderConfig,
    receipt: CapabilityReceipt,
    request: ResidentSpriteRequest,
) -> None:
    if config.normalized_origin != receipt.normalized_origin:
        raise SpriteWorkerError(
            "PROVIDER_ORIGIN_MISMATCH", "Provider origin does not match the capability receipt"
        )
    if request.model != config.model or receipt.model_alias != config.model:
        raise SpriteWorkerError(
            "MODEL_MISMATCH", "Generation request, provider, and capability models differ"
        )


def _eligible(now: datetime):
    return or_(
        ResidentSpriteRun.status.in_(("requested", "retrying", "interrupted")),
        and_(
            ResidentSpriteRun.status == "generating",
            ResidentSpriteRun.lease_expires_at.is_not(None),
            ResidentSpriteRun.lease_expires_at <= now,
        ),
    )


async def claim_next_run(
    db: AsyncSession,
    *,
    owner: str,
    now: datetime,
    lease_seconds: int,
) -> ClaimedSpriteRun | None:
    """Claim one queued/expired run with a compare-and-swap update."""
    for _ in range(5):
        candidate = (await db.execute(
            select(ResidentSpriteRun.id, ResidentSpriteRun.status)
            .where(_eligible(now))
            .order_by(ResidentSpriteRun.created_at.asc())
            .limit(1)
        )).first()
        if candidate is None:
            return None
        run_id, previous_status = candidate
        result = await db.execute(
            update(ResidentSpriteRun)
            .where(ResidentSpriteRun.id == run_id, _eligible(now))
            .values(
                status="generating",
                lease_owner=owner,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempts=ResidentSpriteRun.attempts + 1,
                version=ResidentSpriteRun.version + 1,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            await db.rollback()
            continue
        await db.commit()
        claimed = await db.get(ResidentSpriteRun, run_id)
        if claimed is None:
            return None
        try:
            request = ResidentSpriteRequest.model_validate(claimed.generation_request_json)
        except ValidationError as exc:
            await _finish(
                db, claimed.run_id, owner,
                status="failed", error_code="GENERATION_REQUEST_INVALID",
                error_message="Stored sprite generation request is invalid",
            )
            raise SpriteWorkerError(
                "GENERATION_REQUEST_INVALID", "Stored sprite generation request is invalid"
            ) from exc
        return ClaimedSpriteRun(
            id=claimed.id, run_id=claimed.run_id, request=request, owner=owner,
            attempts=claimed.attempts, previous_status=previous_status,
        )
    return None


def build_runtime(request: ResidentSpriteRequest):
    """Build a provider bound to a valid, non-revoked qualification receipt."""
    required = (
        settings.resident_sprite_provider_base_url,
        settings.resident_sprite_provider_api_key,
        settings.resident_sprite_capability_receipt,
    )
    if not all(required):
        raise SpriteWorkerError(
            "SPRITE_PROVIDER_NOT_CONFIGURED", "Resident sprite provider is not configured"
        )
    receipt_path = Path(settings.resident_sprite_capability_receipt)
    try:
        receipt_path = validate_non_symlink_path(receipt_path, must_exist=True)
        if receipt_path.stat().st_size > 1024 * 1024:
            raise ValueError("receipt too large")
        receipt = CapabilityReceipt.model_validate_json(receipt_path.read_bytes())
        contract = CapabilityContract.model_validate({
            name: getattr(receipt, name) for name in CapabilityContract.model_fields
        })
        revocation_root = (
            Path(settings.resident_sprite_revocation_root)
            if settings.resident_sprite_revocation_root
            else receipt_path.parent / "revocations"
        )
        capability = QualifiedSpriteCapability(
            receipt=receipt,
            contract=contract,
            revocation_path=revocation_root / f"{receipt.receipt_id}.json",
            clock=lambda: datetime.now(UTC),
        )
        validate_capability_receipt(
            receipt, datetime.now(UTC), contract, capability.revocation_path
        )
        config = ProviderConfig(
            base_url=settings.resident_sprite_provider_base_url,
            api_key=settings.resident_sprite_provider_api_key,
            model=settings.resident_sprite_provider_model,
            timeout=settings.resident_sprite_provider_timeout,
            allow_insecure_http_test=settings.resident_sprite_allow_insecure_http_test,
        )
        validate_provider_binding(config, receipt, request)
        provider = ResidentSpriteProvider(config, get_client())
        return provider, capability, receipt.receipt_id
    except SpriteWorkerError:
        raise
    except ResidentSpriteContractError as exc:
        raise SpriteWorkerError(exc.code, str(exc)) from exc
    except Exception as exc:
        raise SpriteWorkerError(
            "CAPABILITY_RECEIPT_INVALID", "Capability receipt is invalid"
        ) from exc


async def _finish(
    db: AsyncSession,
    run_id: str,
    owner: str,
    *,
    status: str,
    error_code: str | None = None,
    error_message: str | None = None,
    values: dict | None = None,
) -> bool:
    updates = dict(values or {})
    updates.update(
        status=status, error_code=error_code, error_message=error_message,
        lease_owner=None, lease_expires_at=None, updated_at=datetime.now(UTC),
        version=ResidentSpriteRun.version + 1,
    )
    result = await db.execute(
        update(ResidentSpriteRun).where(
            ResidentSpriteRun.run_id == run_id,
            ResidentSpriteRun.status == "generating",
            ResidentSpriteRun.lease_owner == owner,
        ).values(**updates)
    )
    await db.commit()
    return result.rowcount == 1


async def _heartbeat(
    session_factory: async_sessionmaker,
    run_id: str,
    owner: str,
    lease_seconds: int,
) -> None:
    interval = max(5.0, lease_seconds / 3)
    while True:
        await asyncio.sleep(interval)
        async with session_factory() as db:
            result = await db.execute(
                update(ResidentSpriteRun).where(
                    ResidentSpriteRun.run_id == run_id,
                    ResidentSpriteRun.status == "generating",
                    ResidentSpriteRun.lease_owner == owner,
                ).values(
                    lease_expires_at=datetime.now(UTC) + timedelta(seconds=lease_seconds),
                    updated_at=datetime.now(UTC),
                )
            )
            await db.commit()
            if result.rowcount != 1:
                return


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def process_one(
    *,
    session_factory: async_sessionmaker = async_session,
    owner: str | None = None,
    now: datetime | None = None,
    lease_seconds: int | None = None,
    runtime_factory: Callable = build_runtime,
    pipeline: Callable[..., Awaitable[ResidentSpriteRunResult]] = generate_resident_sprite,
) -> bool:
    """Claim and execute at most one run. Returns whether work was claimed."""
    owner = owner or worker_owner()
    now = now or datetime.now(UTC)
    lease_seconds = lease_seconds or settings.resident_sprite_worker_lease_seconds
    async with session_factory() as db:
        try:
            claim = await claim_next_run(
                db, owner=owner, now=now, lease_seconds=lease_seconds
            )
        except SpriteWorkerError:
            return True
    if claim is None:
        return False

    receipt_id = None
    heartbeat = asyncio.create_task(
        _heartbeat(session_factory, claim.run_id, owner, lease_seconds),
        name=f"sprite-lease:{claim.run_id}",
    )
    try:
        provider, capability, receipt_id = runtime_factory(claim.request)
        result = await pipeline(
            claim.request,
            client=provider,
            artifact_root=Path(settings.resident_sprite_artifact_dir),
            run_id=claim.run_id,
            capability=capability,
            retry_failed=claim.attempts > 1,
        )
        manifest = load_run(Path(settings.resident_sprite_artifact_dir), claim.run_id)
        request_count = manifest.request_budget.submitted_image_request_count
        request_cost_upper_bound = settings.resident_sprite_request_cost_upper_bound_usd
        common = {
            "manifest_path": result.manifest_path,
            "request_count": request_count,
            "estimated_cost_usd": estimate_cost_upper_bound(
                request_count, request_cost_upper_bound
            ),
            "capability_receipt_id": receipt_id,
        }
        texture = Path(settings.resident_sprite_artifact_dir) / claim.run_id / "candidate/texture.png"
        portrait = Path(settings.resident_sprite_artifact_dir) / claim.run_id / "candidate/portrait.png"
        if texture.is_file() and portrait.is_file():
            common.update(
                candidate_texture_path=str(texture.resolve()),
                candidate_portrait_path=str(portrait.resolve()),
                candidate_texture_sha256=_sha256(texture),
                candidate_portrait_sha256=_sha256(portrait),
            )
        if result.state in {"auto_qc_passed", "candidate_ready"} and not result.qc_findings:
            final_status, code, message = "candidate_ready", None, None
        elif result.state == "failed" and result.error:
            final_status, code, message = "failed", result.error.code, result.error.message
        elif result.state == "quarantined":
            final_status = "quarantined"
            code = result.qc_findings[0].code if result.qc_findings else "AUTOMATED_QC_FAILED"
            message = "Generated candidate failed automated quality checks"
        else:
            final_status, code, message = "interrupted", "GENERATION_INTERRUPTED", "Generation did not reach a candidate"
        async with session_factory() as db:
            await _finish(
                db, claim.run_id, owner, status=final_status,
                error_code=code, error_message=message, values=common,
            )
    except asyncio.CancelledError:
        async with session_factory() as db:
            await _finish(
                db, claim.run_id, owner, status="interrupted",
                error_code="WORKER_INTERRUPTED", error_message="Sprite worker was interrupted",
                values={"capability_receipt_id": receipt_id},
            )
        raise
    except SpriteWorkerError as exc:
        async with session_factory() as db:
            await _finish(
                db, claim.run_id, owner, status="failed",
                error_code=exc.code, error_message=exc.message,
                values={"capability_receipt_id": receipt_id},
            )
    except ResidentSpriteContractError as exc:
        async with session_factory() as db:
            await _finish(
                db, claim.run_id, owner, status="failed",
                error_code=exc.code, error_message=str(exc),
                values={"capability_receipt_id": receipt_id},
            )
    except Exception:
        logger.exception("resident sprite generation failed for run %s", claim.run_id)
        async with session_factory() as db:
            await _finish(
                db, claim.run_id, owner, status="failed",
                error_code="SPRITE_GENERATION_FAILED", error_message="Resident sprite generation failed",
                values={"capability_receipt_id": receipt_id},
            )
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
    return True


async def resident_sprite_worker_loop(stop_event: asyncio.Event | None = None) -> None:
    owner = worker_owner()
    while stop_event is None or not stop_event.is_set():
        try:
            claimed = await process_one(owner=owner)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("resident sprite worker poll failed")
            claimed = False
        if not claimed:
            await asyncio.sleep(settings.resident_sprite_worker_poll_seconds)
