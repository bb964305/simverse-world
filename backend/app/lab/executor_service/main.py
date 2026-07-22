"""Standalone entrypoint for the isolated Lab Executor process."""
from __future__ import annotations

import json
import os
from typing import Mapping

from fastapi import FastAPI

from app.lab.protocol import ExecutorResourceLimits
from app.lab.deployment_identity import DeploymentIdentity
from app.lab.runtime_ref.service_auth import ServiceAuthConfig

from .schemas import ReceiptSignerConfig
from .server import ExecutorServiceConfig, create_app


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value or value != value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError(f"{name} is required and must be canonical")
    return value


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _nonnegative_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _keyring(env: Mapping[str, str], name: str) -> dict[str, str]:
    raw = _required(env, name)
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
            or any(ord(char) < 32 for char in kid)
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


def create_entrypoint_app(environ: Mapping[str, str] | None = None) -> FastAPI:
    env = os.environ if environ is None else environ
    names = (
        "LAB_EXECUTOR_INSTANCE_ID",
        "LAB_EXECUTOR_STORE_PATH",
        "LAB_EXECUTOR_IMAGE",
        "LAB_EXECUTOR_AUTH_ISSUER",
        "LAB_EXECUTOR_AUTH_AUDIENCE",
        "LAB_EXECUTOR_RECEIPT_ISSUER",
        "LAB_EXECUTOR_RECEIPT_AUDIENCE",
        "LAB_EXECUTOR_RECEIPT_CURRENT_KID",
    )
    required = {name: _required(env, name) for name in names}
    if required["LAB_EXECUTOR_AUTH_AUDIENCE"] != "lab-executor":
        raise ValueError("LAB_EXECUTOR_AUTH_AUDIENCE must be lab-executor")
    if required["LAB_EXECUTOR_RECEIPT_AUDIENCE"] != "lab-executor-receipt":
        raise ValueError(
            "LAB_EXECUTOR_RECEIPT_AUDIENCE must be lab-executor-receipt"
        )
    auth_keys = _keyring(env, "LAB_EXECUTOR_AUTH_KEYS_JSON")
    receipt_keys = _keyring(env, "LAB_EXECUTOR_RECEIPT_KEYS_JSON")
    if set(auth_keys.values()) & set(receipt_keys.values()):
        raise ValueError("Executor auth and receipt keyrings must be isolated")
    receipt_kid = required["LAB_EXECUTOR_RECEIPT_CURRENT_KID"]
    receipt_key = receipt_keys.get(receipt_kid)
    if receipt_key is None:
        raise ValueError(
            "LAB_EXECUTOR_RECEIPT_CURRENT_KID is absent from receipt key ring"
        )

    return create_app(
        ExecutorServiceConfig(
            instance_id=required["LAB_EXECUTOR_INSTANCE_ID"],
            store_path=required["LAB_EXECUTOR_STORE_PATH"],
            image=required["LAB_EXECUTOR_IMAGE"],
            runner=env.get("LAB_EXECUTOR_OCI_RUNNER", "docker"),
            user=env.get("LAB_EXECUTOR_OCI_USER", "65534:65534"),
            ingest_base_url=env.get("LAB_EXECUTOR_INGEST_BASE_URL") or None,
            artifact_spool_path=env.get("LAB_EXECUTOR_ARTIFACT_SPOOL_PATH") or None,
            artifact_upload_timeout_seconds=_positive_int(
                env, "LAB_EXECUTOR_ARTIFACT_UPLOAD_TIMEOUT_SECONDS", 30
            ),
            max_concurrent_jobs=_positive_int(
                env, "LAB_EXECUTOR_MAX_CONCURRENT_JOBS", 4
            ),
            max_pending_jobs=_positive_int(
                env, "LAB_EXECUTOR_MAX_PENDING_JOBS", 64
            ),
            service_auth=ServiceAuthConfig(
                issuer=required["LAB_EXECUTOR_AUTH_ISSUER"],
                audience=required["LAB_EXECUTOR_AUTH_AUDIENCE"],
                keys=auth_keys,
                leeway_seconds=_nonnegative_int(
                    env, "LAB_EXECUTOR_AUTH_LEEWAY_SECONDS", 1
                ),
            ),
            receipt_signer=ReceiptSignerConfig(
                issuer=required["LAB_EXECUTOR_RECEIPT_ISSUER"],
                audience=required["LAB_EXECUTOR_RECEIPT_AUDIENCE"],
                current_kid=receipt_kid,
                current_key=receipt_key,
            ),
            deployment_identity=DeploymentIdentity.from_env(
                env, image_digest_name="LAB_EXECUTOR_SERVICE_IMAGE_DIGEST"
            ),
            max_limits=ExecutorResourceLimits(
                wall_clock_ms=_positive_int(
                    env, "LAB_EXECUTOR_MAX_WALL_CLOCK_MS", 120_000
                ),
                cpu_millis=_positive_int(
                    env, "LAB_EXECUTOR_MAX_CPU_MILLIS", 2_000
                ),
                memory_bytes=_positive_int(
                    env, "LAB_EXECUTOR_MAX_MEMORY_BYTES", 1024 * 1024 * 1024
                ),
                pids=_positive_int(env, "LAB_EXECUTOR_MAX_PIDS", 512),
                stdout_bytes=_positive_int(
                    env, "LAB_EXECUTOR_MAX_STDOUT_BYTES", 64 * 1024
                ),
                stderr_bytes=_positive_int(
                    env, "LAB_EXECUTOR_MAX_STDERR_BYTES", 64 * 1024
                ),
                scratch_bytes=_positive_int(
                    env, "LAB_EXECUTOR_MAX_SCRATCH_BYTES", 512 * 1024 * 1024
                ),
            ),
        )
    )


def _disabled_app() -> FastAPI:
    disabled = FastAPI(title="Simverse Lab Executor (not configured)", version="0")

    @disabled.get("/livez", status_code=503)
    async def disabled_livez():
        return {"alive": False, "reason": "executor_not_configured"}

    return disabled


if __name__ == "__main__":
    try:
        app = create_entrypoint_app()
    except ValueError as exc:
        raise SystemExit(f"Executor configuration error: {exc}") from exc
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("LAB_EXECUTOR_BIND_HOST", "0.0.0.0"),
        port=_positive_int(os.environ, "LAB_EXECUTOR_BIND_PORT", 8910),
        log_level=os.environ.get("LAB_EXECUTOR_LOG_LEVEL", "warning"),
    )
else:
    app = _disabled_app()
