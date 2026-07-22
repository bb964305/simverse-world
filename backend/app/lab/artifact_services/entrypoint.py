"""Strict, role-local configuration helpers for Artifact service processes."""
from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence

import uvicorn
from fastapi import FastAPI

from app.lab.artifact_services.auth import JwtKeyring, ServiceTokenValidator
from app.lab.artifact_services.receipts import (
    Ed25519ReceiptSigner,
    HmacReceiptSigner,
    ReceiptSigner,
)
from app.lab.artifact_services.storage.base import ObjectStorage
from app.lab.artifact_services.storage.filesystem import FileSystemStorage
from app.lab.artifact_services.storage.s3 import S3Config, S3SigV4Storage


def required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value or value != value.strip():
        raise ValueError(f"{name} is required and must be canonical")
    return value


def positive_int(
    env: Mapping[str, str], name: str, default: int | None = None
) -> int:
    raw = required(env, name) if default is None else env.get(name, str(default))
    if not raw.isdecimal() or int(raw) <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return int(raw)


def nonnegative_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, str(default))
    if not raw.isdecimal():
        raise ValueError(f"{name} must be a non-negative integer")
    return int(raw)


def positive_float(
    env: Mapping[str, str], name: str, default: float, *, allow_zero: bool = False
) -> float:
    raw = env.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not math.isfinite(value) or (value < 0 if allow_zero else value <= 0):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be {qualifier}")
    return value


def json_string_list(
    env: Mapping[str, str], name: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    raw = required(env, name)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be a JSON string array") from exc
    if (
        not isinstance(value, list)
        or (not value and not allow_empty)
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise ValueError(f"{name} must be a non-empty JSON string array")
    return tuple(value)


def keyring(env: Mapping[str, str], name: str) -> dict[str, str]:
    raw = required(env, name)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be a JSON object") from exc
    if (
        not isinstance(value, dict)
        or len(value) < 2
        or any(
            not isinstance(kid, str)
            or not kid
            or kid != kid.strip()
            or not isinstance(secret, str)
            or len(secret.encode("utf-8")) < 32
            for kid, secret in value.items()
        )
        or len(set(value.values())) != len(value)
    ):
        raise ValueError(
            f"{name} requires distinct current and next keys of at least 32 bytes"
        )
    return value


def service_validator(
    env: Mapping[str, str],
    *,
    prefix: str,
    expected_audience: str,
    max_lifetime_seconds: int = 300,
) -> ServiceTokenValidator:
    audience = required(env, f"{prefix}_AUTH_AUDIENCE")
    if audience != expected_audience:
        raise ValueError(
            f"{prefix}_AUTH_AUDIENCE must be {expected_audience}"
        )
    return ServiceTokenValidator(
        JwtKeyring(
            issuer=required(env, f"{prefix}_AUTH_ISSUER"),
            audience=audience,
            keys=keyring(env, f"{prefix}_AUTH_KEYS_JSON"),
            leeway_seconds=nonnegative_int(
                env, f"{prefix}_AUTH_LEEWAY_SECONDS", 1
            ),
            max_lifetime_seconds=positive_int(
                env,
                f"{prefix}_AUTH_MAX_LIFETIME_SECONDS",
                max_lifetime_seconds,
            ),
        )
    )


def receipt_signer(
    env: Mapping[str, str], *, prefix: str
) -> ReceiptSigner:
    algorithm = required(env, f"{prefix}_RECEIPT_ALGORITHM")
    if algorithm == "EdDSA":
        return Ed25519ReceiptSigner(
            issuer=required(env, f"{prefix}_RECEIPT_ISSUER"),
            current_kid=required(env, f"{prefix}_RECEIPT_CURRENT_KID"),
            private_key_path=required(
                env, f"{prefix}_RECEIPT_PRIVATE_KEY_PATH"
            ),
        )
    if algorithm != "HS256":
        raise ValueError(f"{prefix}_RECEIPT_ALGORITHM is unsupported")
    keys = keyring(env, f"{prefix}_RECEIPT_KEYS_JSON")
    current_kid = required(env, f"{prefix}_RECEIPT_CURRENT_KID")
    current_key = keys.get(current_kid)
    if current_key is None:
        raise ValueError(
            f"{prefix}_RECEIPT_CURRENT_KID is absent from the receipt keyring"
        )
    return HmacReceiptSigner(
        issuer=required(env, f"{prefix}_RECEIPT_ISSUER"),
        current_kid=current_kid,
        current_key=current_key,
    )


def object_storage(
    env: Mapping[str, str],
    *,
    prefix: str,
    zones: Sequence[str],
    read_only_zones: frozenset[str] = frozenset(),
) -> ObjectStorage:
    configured_zones = tuple(zones)
    if not configured_zones or not set(configured_zones).issubset(
        {"quarantine", "released"}
    ):
        raise ValueError("Artifact storage zones are invalid")
    buckets = {
        zone: required(env, f"{prefix}_{zone.upper()}_BUCKET")
        for zone in configured_zones
    }
    backend = required(env, f"{prefix}_STORAGE_BACKEND").lower()
    if backend == "filesystem":
        return FileSystemStorage(
            root=required(env, f"{prefix}_STORAGE_ROOT"),
            buckets=buckets,
            read_only_zones=read_only_zones,
        )
    if backend == "s3":
        return S3SigV4Storage(
            S3Config(
                endpoint_url=required(env, f"{prefix}_S3_ENDPOINT_URL"),
                region=required(env, f"{prefix}_S3_REGION"),
                access_key=required(env, f"{prefix}_S3_ACCESS_KEY"),
                secret_key=required(env, f"{prefix}_S3_SECRET_KEY"),
                session_token=env.get(f"{prefix}_S3_SESSION_TOKEN") or None,
                buckets=buckets,
                timeout_seconds=positive_float(
                    env, f"{prefix}_S3_TIMEOUT_SECONDS", 30.0
                ),
            )
        )
    raise ValueError(f"{prefix}_STORAGE_BACKEND must be filesystem or s3")


def disabled_app(service: str) -> FastAPI:
    app = FastAPI(title=f"Simverse {service} (not configured)", version="0")

    @app.get("/livez", status_code=503)
    async def disabled_livez():
        return {"alive": False, "service": service, "reason": "not_configured"}

    @app.get("/readyz", status_code=503)
    async def disabled_readyz():
        return {"ready": False, "service": service, "reason": "not_configured"}

    return app


def run(app: FastAPI, *, env: Mapping[str, str], prefix: str, port: int) -> None:
    log_level = env.get(f"{prefix}_LOG_LEVEL", "warning").lower()
    if log_level not in {"critical", "error", "warning", "info", "debug"}:
        raise ValueError(f"{prefix}_LOG_LEVEL is invalid")
    uvicorn.run(
        app,
        host=env.get(f"{prefix}_BIND_HOST", "0.0.0.0"),
        port=positive_int(env, f"{prefix}_BIND_PORT", port),
        log_level=log_level,
        workers=1,
    )
