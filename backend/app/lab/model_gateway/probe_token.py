"""Issue a short-lived token for the ARM deployment probe only."""
from __future__ import annotations

import os
import time
import uuid

import jwt

from app.lab.model_catalog import (
    FLASH_MODEL,
    MODEL_GATEWAY_AUDIENCE,
    MODEL_GATEWAY_ISSUER,
    PRO_MODEL,
    RESOURCE_PROFILES,
)


def main() -> None:
    secret = os.environ.get("LAB_MODEL_GATEWAY_AUTH_SECRET", "")
    tier = os.environ.get("LAB_PROBE_MODEL_TIER", "")
    run_id = os.environ.get("LAB_PROBE_RUN_ID", "")
    if len(secret.encode("utf-8")) < 32 or tier not in {"low", "high"} or not run_id:
        raise SystemExit("probe token configuration is invalid")
    now = int(time.time())
    model = FLASH_MODEL if tier == "low" else PRO_MODEL
    print(jwt.encode({
        "iss": MODEL_GATEWAY_ISSUER,
        "aud": MODEL_GATEWAY_AUDIENCE,
        "sub": run_id,
        "jti": str(uuid.uuid4()),
        "tenant_id": "arm-probe",
        "task_id": "arm-probe-task",
        "run_id": run_id,
        "model_tier": tier,
        "model": model,
        "model_policy_version": "arm-probe-v1",
        "budget_usd_cents": 100,
        "resource_cpu_cores": RESOURCE_PROFILES[tier]["cpu_cores"],
        "resource_memory_mb": RESOURCE_PROFILES[tier]["memory_mb"],
        "max_model_tokens": 200_000,
        "iat": now,
        "nbf": now,
        "exp": now + 1800,
    }, secret, algorithm="HS256"))


if __name__ == "__main__":
    main()
