"""Standalone entrypoint for the exact-version Artifact Cleanup trust plane."""
from __future__ import annotations

import os
from collections.abc import Mapping

from fastapi import FastAPI

from app.lab.deployment_identity import DeploymentIdentity
from app.lab.artifact_services.cleanup.server import create_app
from app.lab.artifact_services.cleanup.service import CleanupConfig, CleanupService
from app.lab.artifact_services.entrypoint import (
    disabled_app,
    object_storage,
    positive_int,
    receipt_signer,
    required,
    run,
    service_validator,
)
from app.lab.artifact_services.store import OperationStore


PREFIX = "LAB_ARTIFACT_CLEANUP"


def create_entrypoint_app(environ: Mapping[str, str] | None = None) -> FastAPI:
    env = os.environ if environ is None else environ
    service = CleanupService(
        config=CleanupConfig(
            service_instance_id=required(env, f"{PREFIX}_INSTANCE_ID"),
            claim_seconds=positive_int(env, f"{PREFIX}_CLAIM_SECONDS", 120),
        ),
        store=OperationStore(required(env, f"{PREFIX}_STORE_PATH")),
        storage=object_storage(
            env,
            prefix=PREFIX,
            zones=("quarantine", "released"),
        ),
        receipt_signer=receipt_signer(env, prefix=PREFIX),
    )
    return create_app(
        service=service,
        deployment_identity=DeploymentIdentity.from_env(
            env, image_digest_name=f"{PREFIX}_IMAGE_DIGEST"
        ),
        gateway_auth=service_validator(
            env,
            prefix=PREFIX,
            expected_audience="lab-artifact-cleanup",
        ),
    )


def main() -> None:
    try:
        app = create_entrypoint_app()
        run(app, env=os.environ, prefix=PREFIX, port=8940)
    except ValueError as exc:
        raise SystemExit(f"Artifact Cleanup configuration error: {exc}") from exc


if __name__ == "__main__":
    main()
else:
    app = disabled_app("Artifact Cleanup")
