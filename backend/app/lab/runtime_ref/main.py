"""Production process entrypoint for one stateful Lab Runtime shard.

Run with ``python -m app.lab.runtime_ref.main``. The legacy reference entrypoint
remains in ``server.py`` for local protocol-v1 compatibility.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import anthropic
import uvicorn

from app.lab.deployment_identity import DeploymentIdentity
from app.lab.runtime_ref.agent import anthropic_completer
from app.lab.runtime_ref.server import create_app


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value or value != value.strip():
        raise ValueError(f"{name} is required and must be canonical")
    return value


def _integer(
    env: Mapping[str, str], name: str, default: int, *, minimum: int = 1
) -> int:
    raw = env.get(name, str(default))
    if not raw.isdecimal():
        raise ValueError(f"{name} must be an integer")
    value = int(raw)
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _float(
    env: Mapping[str, str], name: str, default: float, *, minimum: float
) -> float:
    raw = env.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True)
class RuntimeProcessConfig:
    bind_host: str
    bind_port: int
    shard_id: str
    store_path: str
    spool_path: str
    auth_issuer: str
    auth_audience: str
    auth_keys: Mapping[str, str]
    artifact_ingest_base_url: str
    model_api_key: str
    model_base_url: str
    model_name: str
    max_steps: int
    max_active_sessions: int
    max_concurrent_turns: int
    max_queue_depth: int
    max_spool_bytes: int
    max_artifact_bytes: int
    artifact_upload_timeout_seconds: float
    artifact_recovery_interval_seconds: float
    log_level: str
    deployment_identity: DeploymentIdentity

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None
    ) -> "RuntimeProcessConfig":
        env = os.environ if environ is None else environ
        if env.get("LAB_RUNTIME_PROTOCOL_VERSION") != "2":
            raise ValueError(
                "production Runtime requires LAB_RUNTIME_PROTOCOL_VERSION=2"
            )
        raw_keys = _required(env, "LAB_RUNTIME_AUTH_KEYS_JSON")
        try:
            keys = json.loads(raw_keys)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "LAB_RUNTIME_AUTH_KEYS_JSON must be a JSON object"
            ) from exc
        if (
            not isinstance(keys, dict)
            or len(keys) < 2
            or any(
                not isinstance(kid, str)
                or not kid
                or not isinstance(key, str)
                or len(key.encode("utf-8")) < 32
                for kid, key in keys.items()
            )
            or len(set(keys.values())) != len(keys)
        ):
            raise ValueError(
                "Runtime auth keyring requires distinct current and next keys"
            )

        store_path = str(Path(_required(env, "LAB_RUNTIME_STORE_PATH")).expanduser())
        spool_path = str(Path(_required(env, "LAB_RUNTIME_SPOOL_PATH")).expanduser())
        if Path(store_path).resolve() == Path(spool_path).resolve():
            raise ValueError("Runtime store and artifact spool paths must be distinct")
        audience = _required(env, "LAB_RUNTIME_AUTH_AUDIENCE")
        if audience != "lab-runtime":
            raise ValueError("Runtime auth audience must be lab-runtime")
        log_level = env.get("LAB_RUNTIME_LOG_LEVEL", "warning").lower()
        if log_level not in {"critical", "error", "warning", "info", "debug"}:
            raise ValueError("LAB_RUNTIME_LOG_LEVEL is invalid")

        max_spool_bytes = _integer(
            env, "LAB_RUNTIME_MAX_SPOOL_BYTES", 1024 * 1024 * 1024
        )
        max_artifact_bytes = _integer(
            env, "LAB_RUNTIME_MAX_ARTIFACT_BYTES", 64 * 1024 * 1024
        )
        if max_artifact_bytes > max_spool_bytes:
            raise ValueError(
                "LAB_RUNTIME_MAX_ARTIFACT_BYTES must not exceed spool capacity"
            )

        return cls(
            bind_host=env.get("LAB_RUNTIME_BIND_HOST", "0.0.0.0"),
            bind_port=_integer(
                env, "LAB_RUNTIME_BIND_PORT", 8900, minimum=1
            ),
            shard_id=_required(env, "LAB_RUNTIME_SHARD_ID"),
            store_path=store_path,
            spool_path=spool_path,
            auth_issuer=_required(env, "LAB_RUNTIME_AUTH_ISSUER"),
            auth_audience=audience,
            auth_keys=keys,
            artifact_ingest_base_url=_required(
                env, "LAB_RUNTIME_ARTIFACT_INGEST_BASE_URL"
            ),
            model_api_key=_required(env, "LAB_RUNTIME_MODEL_API_KEY"),
            model_base_url=env.get("LAB_RUNTIME_MODEL_BASE_URL", ""),
            model_name=_required(env, "LAB_RUNTIME_MODEL_NAME"),
            max_steps=_integer(env, "LAB_RUNTIME_MAX_STEPS", 3),
            max_active_sessions=_integer(
                env, "LAB_RUNTIME_MAX_ACTIVE_SESSIONS", 100
            ),
            max_concurrent_turns=_integer(
                env, "LAB_RUNTIME_MAX_CONCURRENT_TURNS", 4
            ),
            max_queue_depth=_integer(
                env, "LAB_RUNTIME_MAX_QUEUE_DEPTH", 32, minimum=0
            ),
            max_spool_bytes=max_spool_bytes,
            max_artifact_bytes=max_artifact_bytes,
            artifact_upload_timeout_seconds=_float(
                env, "LAB_RUNTIME_ARTIFACT_UPLOAD_TIMEOUT_S", 300.0, minimum=0.1
            ),
            artifact_recovery_interval_seconds=_float(
                env, "LAB_RUNTIME_ARTIFACT_RECOVERY_INTERVAL_S", 10.0, minimum=0.1
            ),
            log_level=log_level,
            deployment_identity=DeploymentIdentity.from_env(
                env, image_digest_name="LAB_RUNTIME_SERVICE_IMAGE_DIGEST"
            ),
        )


def build_app(config: RuntimeProcessConfig):
    client_kwargs = {"api_key": config.model_api_key}
    if config.model_base_url:
        client_kwargs["base_url"] = config.model_base_url
    model_client = anthropic.AsyncAnthropic(**client_kwargs)
    app = create_app(
        completer_factory=lambda: anthropic_completer(
            model_client, config.model_name
        ),
        max_steps=config.max_steps,
        protocol_version=2,
        runtime_store_path=config.store_path,
        runtime_spool_path=config.spool_path,
        runtime_shard_id=config.shard_id,
        artifact_ingest_base_url=config.artifact_ingest_base_url,
        service_auth={
            "issuer": config.auth_issuer,
            "audience": config.auth_audience,
            "keys": dict(config.auth_keys),
        },
        max_active_sessions=config.max_active_sessions,
        max_concurrent_turns=config.max_concurrent_turns,
        max_queue_depth=config.max_queue_depth,
        max_spool_bytes=config.max_spool_bytes,
        max_artifact_bytes=config.max_artifact_bytes,
        artifact_upload_timeout_seconds=config.artifact_upload_timeout_seconds,
        artifact_recovery_interval_seconds=config.artifact_recovery_interval_seconds,
        deployment_identity=config.deployment_identity,
    )
    app.state.runtime_model_client = model_client
    return app


def main() -> None:
    try:
        config = RuntimeProcessConfig.from_env()
        app = build_app(config)
    except ValueError as exc:
        raise SystemExit(f"Runtime configuration error: {exc}") from exc
    uvicorn.run(
        app,
        host=config.bind_host,
        port=config.bind_port,
        log_level=config.log_level,
        workers=1,
    )


if __name__ == "__main__":
    main()
