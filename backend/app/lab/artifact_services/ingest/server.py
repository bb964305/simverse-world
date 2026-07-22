"""FastAPI surface for Artifact Ingest."""
from __future__ import annotations

import os
import socket
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from app.lab.artifact_services.auth import (
    ArtifactAuthError,
    RequestBinding,
    ServiceTokenValidator,
    UploadCapabilityClaims,
    extract_bearer,
)
from app.lab.artifact_services.ingest.service import IngestError, IngestService
from app.lab.artifact_services.schemas import UploadLeaseCommand
from app.lab.artifact_services.store import OperationConflict


def create_app(
    *,
    service: IngestService,
    gateway_auth: ServiceTokenValidator,
    deployment_identity=None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await service.initialize()
        yield

    app = FastAPI(title="Simverse Artifact Ingest", lifespan=lifespan)

    @app.exception_handler(ArtifactAuthError)
    async def auth_error(_request: Request, exc: ArtifactAuthError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.reason})

    @app.exception_handler(IngestError)
    async def ingest_error(_request: Request, exc: IngestError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.reason})

    @app.exception_handler(OperationConflict)
    async def operation_conflict(_request: Request, _exc: OperationConflict):
        return JSONResponse(status_code=409, content={"detail": "operation_conflict"})

    @app.get("/livez")
    async def livez():
        payload = {
            "alive": True,
            "service": "artifact-ingest",
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
            content={"ready": ready, "service": "artifact-ingest"},
        )

    @app.post("/v1/upload-leases", status_code=201)
    async def create_lease(
        command: UploadLeaseCommand,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ):
        binding = RequestBinding.from_command(command, operation_id=command.upload_id)
        gateway_auth.validate(
            extract_bearer(authorization),
            action="artifact.lease.create",
            binding=binding,
        )
        return (await service.create_upload_lease(command)).model_dump(mode="json")

    @app.put("/v1/uploads/{upload_id}")
    async def upload(
        upload_id: str,
        request: Request,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ):
        token = extract_bearer(authorization)
        # Validate signature/action/path binding before the operation-store lookup.
        claims = service.upload_validator.validate(
            token,
            action="artifact.upload",
            claims_type=UploadCapabilityClaims,
            allow_expired=True,
        )
        if claims.operation_id != upload_id:
            from app.lab.artifact_services.auth import ArtifactAuthorizationError

            raise ArtifactAuthorizationError("upload_path_binding_mismatch")
        receipt = await service.upload(
            upload_id=upload_id,
            chunks=request.stream(),
            token=token,
            owner_id=(
                f"{socket.gethostname()}:{os.getpid()}:"
                f"{service.config.service_instance_id}:{uuid.uuid4()}"
            ),
        )
        return JSONResponse(
            status_code=201 if receipt.status == "completed" else 422,
            content=receipt.model_dump(mode="json"),
        )

    @app.get("/v1/uploads/{upload_id}")
    async def get_upload_receipt(
        upload_id: str,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ):
        token = extract_bearer(authorization)
        claims = gateway_auth.validate(token, action="artifact.upload.read")
        if claims.operation_id != upload_id:
            from app.lab.artifact_services.auth import ArtifactAuthorizationError

            raise ArtifactAuthorizationError("upload_path_binding_mismatch")
        command = await service.get_upload_command(upload_id)
        binding = RequestBinding.from_command(command, operation_id=upload_id)
        gateway_auth.validate(
            token,
            action="artifact.upload.read",
            binding=binding,
        )
        return (await service.get_upload_receipt(upload_id)).model_dump(mode="json")

    return app
