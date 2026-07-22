"""FastAPI surface for exact-version Artifact Cleanup."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from app.lab.artifact_services.auth import (
    ArtifactAuthError,
    ArtifactAuthorizationError,
    RequestBinding,
    ServiceTokenValidator,
    extract_bearer,
)
from app.lab.artifact_services.cleanup.service import CleanupError, CleanupService
from app.lab.artifact_services.schemas import DeleteCommand


def create_app(
    *,
    service: CleanupService,
    gateway_auth: ServiceTokenValidator,
    deployment_identity=None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await service.initialize()
        yield

    app = FastAPI(title="Simverse Artifact Cleanup", lifespan=lifespan)

    @app.exception_handler(ArtifactAuthError)
    async def auth_error(_request: Request, exc: ArtifactAuthError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.reason})

    @app.exception_handler(CleanupError)
    async def cleanup_error(_request: Request, exc: CleanupError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.reason})

    @app.get("/livez")
    async def livez():
        payload = {
            "alive": True,
            "service": "artifact-cleanup",
            "receipt_algorithm": service.receipt_signer.algorithm,
        }
        if deployment_identity is not None:
            payload.update(deployment_identity.health_fields())
        return payload

    @app.get("/readyz")
    async def readyz():
        ready = await service.ready()
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"ready": ready, "service": "artifact-cleanup"},
        )

    @app.post("/v1/deletes")
    async def delete(
        command: DeleteCommand,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ):
        binding = RequestBinding.from_command(
            command, operation_id=command.delete_operation_id
        )
        gateway_auth.validate(
            extract_bearer(authorization),
            action="artifact.delete",
            binding=binding,
        )
        receipt = await service.delete(command)
        return JSONResponse(
            status_code=200 if receipt.status == "completed" else 503,
            content=receipt.model_dump(mode="json"),
        )

    @app.get("/v1/deletes/{delete_operation_id}")
    async def get_delete(
        delete_operation_id: str,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ):
        token = extract_bearer(authorization)
        claims = gateway_auth.validate(token, action="artifact.delete.read")
        if claims.operation_id != delete_operation_id:
            raise ArtifactAuthorizationError("delete_path_binding_mismatch")
        record = await service.store.get(delete_operation_id)
        if record is None or record.kind != "delete":
            raise CleanupError("delete_operation_not_found")
        command = DeleteCommand.model_validate(record.command)
        binding = RequestBinding.from_command(command, operation_id=delete_operation_id)
        gateway_auth.validate(
            token, action="artifact.delete.read", binding=binding
        )
        return (await service.get_receipt(delete_operation_id)).model_dump(mode="json")

    return app
