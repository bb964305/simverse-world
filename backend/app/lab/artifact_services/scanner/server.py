"""FastAPI surface for Artifact Scanner."""
from __future__ import annotations

import asyncio
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
from app.lab.artifact_services.scanner.service import (
    ScannerError,
    ScannerNotFound,
    ScannerService,
)
from app.lab.artifact_services.schemas import ScanCommand


def create_app(
    *,
    service: ScannerService,
    gateway_auth: ServiceTokenValidator,
    deployment_identity=None,
) -> FastAPI:
    stop_event = asyncio.Event()
    worker_task: asyncio.Task | None = None

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal worker_task
        await service.initialize()
        worker_task = asyncio.create_task(service.run_worker(stop_event=stop_event))
        try:
            yield
        finally:
            stop_event.set()
            worker_task.cancel()
            await asyncio.gather(worker_task, return_exceptions=True)

    app = FastAPI(title="Simverse Artifact Scanner", lifespan=lifespan)

    @app.exception_handler(ArtifactAuthError)
    async def auth_error(_request: Request, exc: ArtifactAuthError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.reason})

    @app.exception_handler(ScannerError)
    async def scanner_error(_request: Request, exc: ScannerError):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.reason})

    @app.get("/livez")
    async def livez():
        payload = {
            "alive": True,
            "service": "artifact-scanner",
            "receipt_algorithm": service.receipt_signer.algorithm,
        }
        if deployment_identity is not None:
            payload.update(deployment_identity.health_fields())
        return payload

    @app.get("/readyz")
    async def readyz():
        ready = (
            worker_task is not None
            and not worker_task.done()
            and await service.ready()
        )
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"ready": ready, "service": "artifact-scanner"},
        )

    @app.post("/v1/scans", status_code=202)
    async def submit(
        command: ScanCommand,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ):
        binding = RequestBinding.from_command(command, operation_id=command.scan_job_id)
        gateway_auth.validate(
            extract_bearer(authorization),
            action="artifact.scan.submit",
            binding=binding,
        )
        return (await service.submit(command)).model_dump(mode="json")

    @app.get("/v1/scans/{scan_job_id}")
    async def get_scan(
        scan_job_id: str,
        authorization: str | None = Header(default=None, alias="Authorization"),
    ):
        token = extract_bearer(authorization)
        claims = gateway_auth.validate(token, action="artifact.scan.read")
        if claims.operation_id != scan_job_id:
            raise ArtifactAuthorizationError("scan_path_binding_mismatch")
        record = await service.store.get(scan_job_id)
        if record is None or record.kind != "scan":
            raise ScannerNotFound("scan_job_not_found")
        command = ScanCommand.model_validate(record.command)
        binding = RequestBinding.from_command(command, operation_id=scan_job_id)
        gateway_auth.validate(token, action="artifact.scan.read", binding=binding)
        return (await service.get_receipt(scan_job_id)).model_dump(mode="json")

    return app
