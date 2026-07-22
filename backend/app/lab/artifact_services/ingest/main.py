"""Standalone entrypoint for the Artifact Ingest trust plane."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI

from app.lab.deployment_identity import DeploymentIdentity
from app.lab.artifact_services.auth import (
    JwtIssuerConfig,
    JwtKeyring,
    ServiceTokenValidator,
    UploadCapabilityIssuer,
)
from app.lab.artifact_services.entrypoint import (
    disabled_app,
    keyring,
    object_storage,
    positive_int,
    receipt_signer,
    required,
    run,
    service_validator,
)
from app.lab.artifact_services.ingest.server import create_app
from app.lab.artifact_services.ingest.service import IngestConfig, IngestService
from app.lab.artifact_services.store import OperationStore


PREFIX = "LAB_ARTIFACT_INGEST"


def create_entrypoint_app(environ: Mapping[str, str] | None = None) -> FastAPI:
    env = os.environ if environ is None else environ
    lease_ttl = positive_int(env, f"{PREFIX}_MAX_LEASE_TTL_SECONDS", 300)
    upload_keys = keyring(env, f"{PREFIX}_UPLOAD_KEYS_JSON")
    upload_kid = required(env, f"{PREFIX}_UPLOAD_CURRENT_KID")
    upload_key = upload_keys.get(upload_kid)
    if upload_key is None:
        raise ValueError(
            f"{PREFIX}_UPLOAD_CURRENT_KID is absent from the upload keyring"
        )
    upload_audience = required(env, f"{PREFIX}_UPLOAD_AUDIENCE")
    if upload_audience != "lab-artifact-upload":
        raise ValueError(
            f"{PREFIX}_UPLOAD_AUDIENCE must be lab-artifact-upload"
        )
    upload_issuer = required(env, f"{PREFIX}_UPLOAD_ISSUER")

    service = IngestService(
        config=IngestConfig(
            service_instance_id=required(env, f"{PREFIX}_INSTANCE_ID"),
            quarantine_bucket=required(env, f"{PREFIX}_QUARANTINE_BUCKET"),
            spool_dir=Path(required(env, f"{PREFIX}_SPOOL_PATH")),
            max_upload_bytes=positive_int(
                env, f"{PREFIX}_MAX_UPLOAD_BYTES", 100 * 1024 * 1024
            ),
            max_lease_ttl_seconds=lease_ttl,
            upload_claim_seconds=positive_int(
                env, f"{PREFIX}_UPLOAD_CLAIM_SECONDS", 900
            ),
        ),
        store=OperationStore(required(env, f"{PREFIX}_STORE_PATH")),
        storage=object_storage(env, prefix=PREFIX, zones=("quarantine",)),
        receipt_signer=receipt_signer(env, prefix=PREFIX),
        upload_issuer=UploadCapabilityIssuer(
            JwtIssuerConfig(
                issuer=upload_issuer,
                audience=upload_audience,
                current_kid=upload_kid,
                current_key=upload_key,
                ttl_seconds=lease_ttl,
            )
        ),
        upload_validator=ServiceTokenValidator(
            JwtKeyring(
                issuer=upload_issuer,
                audience=upload_audience,
                keys=upload_keys,
                max_lifetime_seconds=lease_ttl,
            )
        ),
    )
    return create_app(
        service=service,
        deployment_identity=DeploymentIdentity.from_env(
            env, image_digest_name=f"{PREFIX}_IMAGE_DIGEST"
        ),
        gateway_auth=service_validator(
            env,
            prefix=PREFIX,
            expected_audience="lab-artifact-ingest",
        ),
    )


def main() -> None:
    try:
        app = create_entrypoint_app()
        run(app, env=os.environ, prefix=PREFIX, port=8920)
    except ValueError as exc:
        raise SystemExit(f"Artifact Ingest configuration error: {exc}") from exc


if __name__ == "__main__":
    main()
else:
    app = disabled_app("Artifact Ingest")
