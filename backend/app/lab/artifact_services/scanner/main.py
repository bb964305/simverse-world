"""Standalone entrypoint for the Artifact Scanner trust plane."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI

from app.lab.deployment_identity import DeploymentIdentity
from app.lab.artifact_services.entrypoint import (
    disabled_app,
    json_string_list,
    nonnegative_int,
    object_storage,
    positive_float,
    positive_int,
    receipt_signer,
    required,
    run,
    service_validator,
)
from app.lab.artifact_services.scanner.policy import ScanPolicy, ScanPolicyConfig
from app.lab.artifact_services.scanner.server import create_app
from app.lab.artifact_services.scanner.service import ScannerConfig, ScannerService
from app.lab.artifact_services.store import OperationStore


PREFIX = "LAB_ARTIFACT_SCANNER"


def create_entrypoint_app(environ: Mapping[str, str] | None = None) -> FastAPI:
    env = os.environ if environ is None else environ
    max_object_bytes = positive_int(
        env, f"{PREFIX}_MAX_OBJECT_BYTES", 100 * 1024 * 1024
    )
    service = ScannerService(
        config=ScannerConfig(
            service_instance_id=required(env, f"{PREFIX}_INSTANCE_ID"),
            released_bucket=required(env, f"{PREFIX}_RELEASED_BUCKET"),
            work_dir=Path(required(env, f"{PREFIX}_WORK_PATH")),
            max_object_bytes=max_object_bytes,
            max_attempts=positive_int(env, f"{PREFIX}_MAX_ATTEMPTS", 3),
            retry_backoff_seconds=positive_float(
                env, f"{PREFIX}_RETRY_BACKOFF_SECONDS", 5.0, allow_zero=True
            ),
            claim_seconds=positive_int(env, f"{PREFIX}_CLAIM_SECONDS", 300),
            poll_seconds=positive_float(env, f"{PREFIX}_POLL_SECONDS", 1.0),
            policy_timeout_seconds=positive_float(
                env, f"{PREFIX}_POLICY_TIMEOUT_SECONDS", 120.0
            ),
        ),
        store=OperationStore(required(env, f"{PREFIX}_STORE_PATH")),
        storage=object_storage(
            env,
            prefix=PREFIX,
            zones=("quarantine", "released"),
            read_only_zones=frozenset({"quarantine"}),
        ),
        policy=ScanPolicy(
            ScanPolicyConfig(
                policy_version=required(env, f"{PREFIX}_POLICY_VERSION"),
                engine_version=required(env, f"{PREFIX}_ENGINE_VERSION"),
                allowed_content_types=frozenset(
                    json_string_list(env, f"{PREFIX}_ALLOWED_CONTENT_TYPES_JSON")
                ),
                malware_command=json_string_list(
                    env, f"{PREFIX}_MALWARE_COMMAND_JSON"
                ),
                malware_timeout_seconds=positive_float(
                    env, f"{PREFIX}_MALWARE_TIMEOUT_SECONDS", 60.0
                ),
                max_file_bytes=positive_int(
                    env, f"{PREFIX}_MAX_FILE_BYTES", max_object_bytes
                ),
                max_archive_depth=nonnegative_int(
                    env, f"{PREFIX}_MAX_ARCHIVE_DEPTH", 3
                ),
                max_archive_files=positive_int(
                    env, f"{PREFIX}_MAX_ARCHIVE_FILES", 1_000
                ),
                max_archive_expanded_bytes=positive_int(
                    env,
                    f"{PREFIX}_MAX_ARCHIVE_EXPANDED_BYTES",
                    500 * 1024 * 1024,
                ),
                max_nested_archive_bytes=positive_int(
                    env,
                    f"{PREFIX}_MAX_NESTED_ARCHIVE_BYTES",
                    32 * 1024 * 1024,
                ),
                max_archive_ratio=positive_float(
                    env, f"{PREFIX}_MAX_ARCHIVE_RATIO", 100.0
                ),
                parser_timeout_seconds=positive_float(
                    env, f"{PREFIX}_PARSER_TIMEOUT_SECONDS", 30.0
                ),
                parser_max_memory_bytes=positive_int(
                    env, f"{PREFIX}_PARSER_MAX_MEMORY_BYTES", 512 * 1024 * 1024
                ),
                max_image_pixels=positive_int(
                    env, f"{PREFIX}_MAX_IMAGE_PIXELS", 100_000_000
                ),
                max_image_decoded_bytes=positive_int(
                    env,
                    f"{PREFIX}_MAX_IMAGE_DECODED_BYTES",
                    512 * 1024 * 1024,
                ),
                max_text_field_bytes=positive_int(
                    env, f"{PREFIX}_MAX_TEXT_FIELD_BYTES", 1024 * 1024
                ),
                max_csv_columns=positive_int(
                    env, f"{PREFIX}_MAX_CSV_COLUMNS", 4096
                ),
            )
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
            expected_audience="lab-artifact-scanner",
        ),
    )


def main() -> None:
    try:
        app = create_entrypoint_app()
        run(app, env=os.environ, prefix=PREFIX, port=8930)
    except ValueError as exc:
        raise SystemExit(f"Artifact Scanner configuration error: {exc}") from exc


if __name__ == "__main__":
    main()
else:
    app = disabled_app("Artifact Scanner")
