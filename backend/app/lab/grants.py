"""Signed run-scoped capability grants (HMAC) — issuance, verification,
attenuated delegation, and revocation (PRD §Run-scoped Grant, §Capability and
Approval Model). Pure signing/lifecycle logic: this module decides nothing
about what a tool call may do — that's ``app.lab.policy``, composed by the
Tool Broker (T3) on top of a verified, still-active grant.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, UTC

from pydantic import ValidationError
from sqlalchemy import select

from app.config import settings
from app.lab.protocol import GrantClaims, canonical_json
from app.models.lab_grant import LabCapabilityGrant

GRANT_ISSUER = "lab-runtime"
GRANT_AUDIENCE = "tool-broker"

# 8 budget dimensions (bare column names shared with GrantClaims.budgets /
# LabRunBudget's limit_*/used_*/reserved_* triplets).
_BUDGET_DIMENSIONS = (
    "model_tokens", "tool_calls", "wall_clock_ms", "egress_requests",
    "egress_bytes", "artifact_count", "artifact_bytes", "active_workers",
)


class GrantError(Exception):
    """A grant failed to verify (bad signature/format), is expired/not-yet-
    valid, is no longer active (revoked/unknown jti/stale fencing epoch), or
    a delegation-attenuation rule (depth/capabilities/egress/budgets/exp/
    tenant) was violated."""


def _secret() -> bytes:
    return (settings.lab_grant_secret or settings.jwt_secret).encode("utf-8")


def _default_budgets() -> dict[str, int]:
    return {dim: getattr(settings, f"lab_budget_{dim}") for dim in _BUDGET_DIMENSIONS}


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hmac_hex(payload_b64: str) -> str:
    return hmac.new(_secret(), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()


def sign_grant(claims: GrantClaims) -> str:
    """token = base64url(canonical_json(claims)) + "." + hmac_sha256_hex(secret, that base64url string)."""
    payload_b64 = _b64url_encode(canonical_json(claims.model_dump(mode="json")).encode("utf-8"))
    return f"{payload_b64}.{_hmac_hex(payload_b64)}"


def grant_hash(token: str) -> str:
    """sha256 of the full signed token — stored on the grant row (``grant_hash``)
    so a presented token can be matched to its DB record without re-verifying."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_grant(token: str, *, now: int | None = None) -> GrantClaims:
    try:
        payload_b64, sig = token.split(".", 1)
    except ValueError:
        raise GrantError("malformed grant token") from None

    # Constant-time comparison: a signature mismatch (from a byte flipped in
    # either the payload or the signature) must not leak timing information.
    if not hmac.compare_digest(sig, _hmac_hex(payload_b64)):
        raise GrantError("bad grant signature")

    try:
        data = json.loads(_b64url_decode(payload_b64))
        claims = GrantClaims.model_validate(data)
    except (ValueError, ValidationError) as exc:
        raise GrantError(f"malformed grant payload: {exc}") from exc

    now = int(time.time()) if now is None else now
    if now >= claims.exp:
        raise GrantError("grant expired")
    if now < claims.nbf:
        raise GrantError("grant not yet valid")
    return claims


async def issue_run_grant(
    db,
    *,
    tenant_id: str,
    task_id: str,
    run_id: str,
    agent_id: str,
    capabilities: list[str],
    egress: list[str] | None = None,
    budgets: dict[str, int] | None = None,
    policy_version: str | None = None,
    fencing_epoch: int = 0,
    parent: GrantClaims | None = None,
    ttl_s: int | None = None,
) -> tuple[str, GrantClaims]:
    """Issue and persist a signed run-scoped grant. With ``parent`` given,
    issues an attenuated child (delegation depth 1): its capabilities/egress
    must be subsets of the parent's, its budgets must not exceed the parent's
    per-dimension, its expiry cannot outlive the parent's, and it must share
    the parent's tenant/task/run — otherwise ``GrantError``."""
    now = int(time.time())
    ttl = settings.lab_grant_ttl_s if ttl_s is None else ttl_s
    egress = [] if egress is None else egress
    budgets = _default_budgets() if budgets is None else budgets
    policy_version = settings.lab_policy_version if policy_version is None else policy_version
    exp = now + ttl
    depth = 0
    parent_jti = None

    if parent is not None:
        depth = parent.depth + 1
        if depth > 1:
            raise GrantError("delegation depth exceeded")
        if tenant_id != parent.tenant_id or task_id != parent.task_id or run_id != parent.run_id:
            raise GrantError("child grant tenant/task/run must match parent")
        if not set(capabilities).issubset(parent.capabilities):
            raise GrantError("child capabilities exceed parent grant")
        if not set(egress).issubset(parent.egress):
            raise GrantError("child egress exceeds parent grant")
        for dim, value in budgets.items():
            if value > parent.budgets.get(dim, 0):
                raise GrantError(f"child budget '{dim}' exceeds parent grant")
        exp = min(exp, parent.exp)
        parent_jti = parent.jti

    claims = GrantClaims(
        iss=GRANT_ISSUER, aud=GRANT_AUDIENCE, jti=str(uuid.uuid4()),
        tenant_id=tenant_id, task_id=task_id, run_id=run_id, agent_id=agent_id,
        parent_jti=parent_jti, depth=depth, capabilities=capabilities,
        egress=egress, budgets=budgets, policy_version=policy_version,
        fencing_epoch=fencing_epoch, nbf=now, exp=exp,
    )
    token = sign_grant(claims)

    row = LabCapabilityGrant(
        jti=claims.jti, tenant_id=tenant_id, task_id=task_id, run_id=run_id,
        agent_id=agent_id, parent_jti=parent_jti, depth=depth, audience=GRANT_AUDIENCE,
        capabilities_json=capabilities, resources_json=claims.resources, egress_json=egress,
        budgets_json=budgets, policy_version=policy_version, fencing_epoch=fencing_epoch,
        nbf=now, exp=exp, grant_hash=grant_hash(token),
    )
    db.add(row)
    await db.commit()
    return token, claims


async def revoke_grant(db, jti: str) -> None:
    row = await db.get(LabCapabilityGrant, jti)
    if row is not None and row.revoked_at is None:
        row.revoked_at = datetime.now(UTC)
        await db.commit()


async def revoke_run_grants(db, run_id: str) -> None:
    result = await db.execute(
        select(LabCapabilityGrant).where(
            LabCapabilityGrant.run_id == run_id,
            LabCapabilityGrant.revoked_at.is_(None),
        )
    )
    now = datetime.now(UTC)
    for row in result.scalars():
        row.revoked_at = now
    await db.commit()


async def check_grant_active(db, claims: GrantClaims, *, expected_epoch: int | None = None) -> None:
    row = await db.get(LabCapabilityGrant, claims.jti)
    if row is None:
        raise GrantError("grant not found")
    if row.revoked_at is not None:
        raise GrantError("grant revoked")
    if expected_epoch is not None and claims.fencing_epoch != expected_epoch:
        raise GrantError("stale epoch")
