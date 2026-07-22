"""FastAPI application for the independent restricted egress worker."""
from __future__ import annotations

import hmac
import logging
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .config import EgressConfig
from .engine import EgressEngine, EgressToolError
from .models import EgressActionCommand, EgressUsage
from .store import EgressActionStore, EgressStoreConflict


logger = logging.getLogger(__name__)


def create_app(
    config: EgressConfig,
    *,
    store: EgressActionStore | None = None,
    engine: EgressEngine | None = None,
) -> FastAPI:
    config.validate_service()
    action_store = store or EgressActionStore(
        config.database_path,
        lease_seconds=config.action_lease_s,
        max_attempts=config.max_attempts,
    )
    egress = engine or EgressEngine(config)
    app = FastAPI(title="Simverse Lab Egress", version="1.0")

    def authenticate(authorization: str | None) -> None:
        prefix = "Bearer "
        if not authorization or not authorization.startswith(prefix):
            raise HTTPException(status_code=401, detail="unauthorized")
        if not hmac.compare_digest(authorization[len(prefix):], config.api_key):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.get("/livez")
    async def livez():
        return {
            "alive": True,
            "service": "lab-egress",
            "protocol_version": 1,
        }

    @app.get("/readyz")
    async def readyz():
        ready = await action_store.health()
        payload = {
            "ready": ready,
            "service": "lab-egress",
            "search_available": config.search_available,
            "fetch_available": config.fetch_available,
        }
        return payload if ready else JSONResponse(status_code=503, content=payload)

    @app.get("/v1/actions/{action_id}")
    async def get_action(
        action_id: str,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        authenticate(authorization)
        if not action_id or len(action_id) > 200:
            raise HTTPException(status_code=404, detail="action_not_found")
        status = await action_store.get(action_id)
        if status is None:
            raise HTTPException(status_code=404, detail="action_not_found")
        return status.model_dump(mode="json")

    @app.post("/v1/actions")
    async def execute_action(
        command: EgressActionCommand,
        authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    ):
        authenticate(authorization)
        try:
            claim = await action_store.claim(command)
        except EgressStoreConflict as exc:
            raise HTTPException(status_code=409, detail="action_binding_conflict") from exc
        if claim.lease_token is None:
            status_code = 200 if claim.status.state in {"succeeded", "failed"} else 202
            return JSONResponse(
                status_code=status_code,
                content=claim.status.model_dump(mode="json"),
            )
        try:
            result, usage = await egress.execute(
                command.tool_name,
                command.args,
                allowlist=command.egress_allowlist,
            )
            status = await action_store.complete(
                action_id=command.action_id,
                lease_token=claim.lease_token,
                result=result,
                error_code=None,
                usage=usage,
            )
        except EgressToolError as exc:
            status = await action_store.complete(
                action_id=command.action_id,
                lease_token=claim.lease_token,
                result=None,
                error_code=exc.code,
                usage=exc.usage,
            )
        except EgressStoreConflict as exc:
            raise HTTPException(status_code=409, detail="action_lease_conflict") from exc
        except Exception:
            logger.exception(
                "egress handler failed unexpectedly",
                extra={"action_id": command.action_id, "tool": command.tool_name},
            )
            status = await action_store.complete(
                action_id=command.action_id,
                lease_token=claim.lease_token,
                result=None,
                error_code="internal_handler_error",
                usage=EgressUsage(),
            )
        return status.model_dump(mode="json")

    return app
