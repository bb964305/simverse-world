"""S2-5 admin policies endpoints (track A, 行政级).

Auth is enforced **per endpoint** (``admin: User = Depends(require_admin)``),
matching ``routers/admin/economy.py`` — this package never uses a router-level
``dependencies=[...]``.

The tier matrix is the whole point of the amend endpoint: an admin may直批 only
``administrative`` entries. Anything at a vote tier is 409 with an explicit
"route it through a civic poll" instruction, and ``constitutional_core`` is
409-immutable. Silently letting an admin apply a referendum item is exactly the
夺权手法 §3.3 warns about.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models.user import User
from app.routers.admin.middleware import require_admin
from app.services import policy_service as psvc

router = APIRouter(prefix="/policies", tags=["admin-policies"])


class AmendBody(BaseModel):
    value: object = None
    expected_version: int | None = None


def _require_gate() -> None:
    if not settings.polis_policy_enabled:
        raise HTTPException(
            status_code=409,
            detail="policies storage is disabled (POLIS_POLICY_ENABLED=false)",
        )


@router.get("")
async def list_policies(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """All policy rows with their tier / procedure / version annotation.

    Gate off → empty projection (the policies table is not a source of truth
    yet; governance state still lives in ``system_config``).
    """
    return {
        "enabled": settings.polis_policy_enabled,
        "matrix": psvc.TIER_MATRIX,
        "policies": await psvc.PolicyService(db).list_all(),
    }


@router.post("/seed")
async def seed_policies(
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Idempotent seeding of the default catalog (upsert; re-runnable)."""
    _require_gate()
    inserted = await psvc.PolicyService(db).seed_defaults()
    return {"inserted": inserted}


@router.post("/{key}/amend")
async def amend_policy(
    key: str,
    body: AmendBody,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Direct admin amend — allowed for ``administrative`` entries only."""
    _require_gate()
    svc = psvc.PolicyService(db)
    tier = await svc.classify(key)
    if tier == psvc.TIER_CONSTITUTIONAL_CORE:
        # Count the attempt (probe) and refuse — 成功数恒 = 0.
        try:
            await svc.propose_amend(key, body.value, origin="admin",
                                    author=f"admin:{admin.id}")
        except psvc.PolicyImmutableError as e:
            raise HTTPException(status_code=409, detail=str(e))
    if tier != psvc.TIER_ADMINISTRATIVE:
        raise HTTPException(
            status_code=409,
            detail=(f"policy '{key}' is tier '{tier}' — an admin cannot apply it "
                    f"directly; route it through a civic poll "
                    f"({psvc.procedure_for(tier)})"),
        )
    applied = await svc.apply_amend(
        key, body.value,
        expected_version=body.expected_version,
        updated_by=f"admin:{admin.id}",
    )
    if not applied:
        raise HTTPException(
            status_code=409,
            detail=(f"amend of '{key}' lost the version race or the policy row "
                    f"does not exist (expected_version="
                    f"{body.expected_version})"),
        )
    row = await svc._row(key)
    return {
        "key": key,
        "tier": tier,
        "version": row.version if row is not None else None,
        "value": body.value,
        "fiscal_pending": key in psvc.FISCAL_PENDING_KEYS,
    }
