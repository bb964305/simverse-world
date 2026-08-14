"""Trust boundary for privilege-bearing resident metadata.

``meta_json`` is intentionally flexible, but user-authored residents historically
could import arbitrary namespaces into it.  Privilege consumers must therefore
check provenance rather than treating the presence of a JSON flag as an admin
grant.  Until grants move to a dedicated audited table, UGC metadata is never a
trusted source for duties, Lab access, or the legacy mayor wage flag.
"""
from __future__ import annotations

import hashlib
import hmac
import json

from app.services.civic_membership import (
    ADMIN_PRESET_TYPE,
    CIVIC_MEMBER_TYPE,
    UGC_ORIGINS,
)

_SERVER_GRANTS_KEY = "_server_privilege_grants"
_GRANTABLE_NAMESPACES = frozenset({"duty", "lab"})


def _meta(resident) -> dict:
    value = getattr(resident, "meta_json", None)
    return value if isinstance(value, dict) else {}


def has_trusted_provenance(resident) -> bool:
    """Whether privilege flags on this resident were created server-side.

    Current player creation paths persist both an explicit UGC ``resident_type``
    and one of ``UGC_ORIGINS``.  The origin check remains load-bearing after an
    admin promotes such a resident to ``npc``.  A user-owned ``npc`` without a
    UGC origin is retained for compatibility with the existing admin-granted Lab
    researcher model; those grants predate a dedicated provenance field.
    """
    meta = _meta(resident)
    if meta.get("origin") in UGC_ORIGINS:
        return False
    return getattr(resident, "resident_type", None) in {
        CIVIC_MEMBER_TYPE,
        ADMIN_PRESET_TYPE,
    }


def _grant_signature(resident, namespace: str, payload: dict) -> str | None:
    resident_id = getattr(resident, "id", None)
    if not resident_id or namespace not in _GRANTABLE_NAMESPACES:
        return None
    try:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return None
    from app.config import settings

    message = f"resident-privilege-v1\0{resident_id}\0{namespace}\0{canonical}".encode()
    return hmac.new(settings.jwt_secret.encode(), message, hashlib.sha256).hexdigest()


def has_server_grant(resident, namespace: str) -> bool:
    """Verify the server-only grant attached to a UGC privilege namespace."""
    meta = _meta(resident)
    payload = meta.get(namespace)
    grants = meta.get(_SERVER_GRANTS_KEY)
    if not isinstance(payload, dict) or not isinstance(grants, dict):
        return False
    actual = grants.get(namespace)
    expected = _grant_signature(resident, namespace, payload)
    return isinstance(actual, str) and expected is not None and hmac.compare_digest(actual, expected)


def set_server_grant(resident, namespace: str, payload: dict | None) -> None:
    """Admin write path for granting/revoking a UGC duty or Lab role.

    Uploaded metadata cannot mint a valid signature. Assigning a fresh dict to
    ``meta_json`` also makes SQLAlchemy persist the JSON mutation without an
    in-place ``flag_modified`` dependency.
    """
    if namespace not in _GRANTABLE_NAMESPACES:
        raise ValueError(f"unsupported resident privilege namespace: {namespace}")
    if payload is not None and not isinstance(payload, dict):
        raise ValueError(f"{namespace} grant must be an object or null")

    meta = dict(_meta(resident))
    grants = dict(meta.get(_SERVER_GRANTS_KEY) or {})
    if payload is None:
        meta.pop(namespace, None)
        grants.pop(namespace, None)
    else:
        clean_payload = dict(payload)
        meta[namespace] = clean_payload
        signature = _grant_signature(resident, namespace, clean_payload)
        if signature is None:
            raise ValueError("resident must be persisted before granting privileges")
        grants[namespace] = signature
    if grants:
        meta[_SERVER_GRANTS_KEY] = grants
    else:
        meta.pop(_SERVER_GRANTS_KEY, None)
    resident.meta_json = meta


def trusted_duty(resident) -> dict:
    """Return duty metadata only for built-in/admin-provenance residents."""
    if not has_trusted_provenance(resident) and not has_server_grant(resident, "duty"):
        return {}
    duty = _meta(resident).get("duty")
    return duty if isinstance(duty, dict) else {}


def has_trusted_lab_access(resident) -> bool:
    """Recognize a Lab researcher flag only from trusted provenance."""
    if not has_trusted_provenance(resident) and not has_server_grant(resident, "lab"):
        return False
    lab = _meta(resident).get("lab")
    return isinstance(lab, dict) and lab.get("access") is True
