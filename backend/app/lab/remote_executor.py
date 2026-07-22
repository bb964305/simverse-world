"""Fail-closed Runner client for the independent Lab Executor service."""
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

import httpx
from pydantic import ValidationError

from app.http import get_client
from app.lab import protocol
from app.lab.executor_service.schemas import (
    EXECUTOR_TERMINAL_STATES,
    ExecutorArtifactEnvelope,
    ExecutorJobResultEnvelope,
    ExecutorJobStatus,
    ReceiptValidationError,
    ReceiptVerifier,
    ReceiptVerifierConfig,
    deterministic_job_id,
)
from app.lab.runtime_ref.service_auth import (
    ServiceTokenIssuer,
    ServiceTokenIssuerConfig,
    canonical_request_digest,
)


class RemoteExecutorError(RuntimeError):
    pass


class RemoteExecutorProtocolError(RemoteExecutorError):
    pass


class RemoteExecutorConflict(RemoteExecutorProtocolError):
    pass


class RemoteExecutorFenced(RemoteExecutorProtocolError):
    pass


class RemoteExecutorUncertainOutcome(RemoteExecutorError):
    """The request may have reached Executor; callers must query the same job."""


@dataclass(frozen=True)
class ExecutorJobBinding:
    job_id: str
    run_id: str
    session_id: str
    action_id: str
    epoch: int
    command_digest: str
    request_digest: str

    @classmethod
    def from_command(
        cls, command: protocol.ExecutorJobCommand
    ) -> "ExecutorJobBinding":
        return cls(
            job_id=command.job_id,
            run_id=command.run_id,
            session_id=command.session_id,
            action_id=command.action_id,
            epoch=command.epoch,
            command_digest=command.command_digest,
            request_digest=canonical_request_digest(command),
        )

    @classmethod
    def from_locator(cls, locator: Mapping[str, Any]) -> "ExecutorJobBinding":
        try:
            binding = cls(
                job_id=locator["job_id"],
                run_id=locator["run_id"],
                session_id=locator["session_id"],
                action_id=locator["action_id"],
                epoch=locator["epoch"],
                command_digest=locator["command_digest"],
                request_digest=locator["request_digest"],
            )
        except (KeyError, TypeError) as exc:
            raise RemoteExecutorProtocolError("executor locator is incomplete") from exc
        if (
            any(
                not isinstance(getattr(binding, name), str)
                or not getattr(binding, name)
                for name in (
                    "job_id",
                    "run_id",
                    "session_id",
                    "action_id",
                    "command_digest",
                    "request_digest",
                )
            )
            or type(binding.epoch) is not int
            or binding.epoch < 0
            or binding.job_id
            != deterministic_job_id(binding.action_id, binding.epoch)
            or not _is_sha256(binding.command_digest)
            or not _is_sha256(binding.request_digest)
        ):
            raise RemoteExecutorProtocolError("executor locator binding is invalid")
        return binding


class RemoteExecutorClient:
    def __init__(
        self,
        *,
        base_url: str,
        token_issuer: ServiceTokenIssuer,
        receipt_verifier: ReceiptVerifier,
        timeout: float = 30.0,
        poll_interval: float = 0.25,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        normalized = (base_url or "").rstrip("/")
        if not normalized:
            raise ValueError("remote Executor base_url is required")
        if token_issuer.config.audience != "lab-executor":
            raise ValueError("remote Executor token audience must be lab-executor")
        if timeout <= 0 or poll_interval <= 0:
            raise ValueError("Executor timeout and poll interval must be positive")
        self.base_url = normalized
        self.token_issuer = token_issuer
        self.receipt_verifier = receipt_verifier
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._http_client = http_client

    @property
    def http_client(self) -> httpx.AsyncClient:
        return self._http_client or get_client()

    async def ready(self) -> bool:
        """Read the service readiness gate used before production admission."""
        try:
            response = await self.http_client.get(
                f"{self.base_url}/readyz", timeout=self.timeout
            )
        except (httpx.TimeoutException, httpx.TransportError):
            return False
        if response.status_code != 200 or len(response.content) > 16 * 1024:
            return False
        try:
            payload = response.json()
            return (
                isinstance(payload, dict)
                and payload.get("schema_version") == 1
                and payload.get("ready") is True
                and isinstance(payload.get("instance_id"), str)
                and bool(payload["instance_id"])
            )
        except (ValueError, UnicodeError, RecursionError):
            return False

    @staticmethod
    def build_command(
        *,
        run_id: str,
        session_id: str,
        action_id: str,
        epoch: int,
        tool_name: str,
        args: dict[str, Any],
        image_digest: str,
        limits: protocol.ExecutorResourceLimits,
        deadline_at: datetime,
        outputs: list[protocol.ExecutorOutputSpec] | None = None,
    ) -> protocol.ExecutorJobCommand:
        job_id = deterministic_job_id(action_id, epoch)
        candidate = {
            "schema_version": 1,
            "job_id": job_id,
            "run_id": run_id,
            "session_id": session_id,
            "action_id": action_id,
            "epoch": epoch,
            "tool_name": tool_name,
            "args": args,
            "image_digest": image_digest,
            "limits": limits.model_dump(mode="json"),
            "outputs": [
                output.model_dump(mode="json") for output in (outputs or [])
            ],
            "deadline_at": deadline_at.isoformat().replace("+00:00", "Z"),
        }
        return protocol.ExecutorJobCommand.model_validate(
            {**candidate, "command_digest": protocol.content_digest(candidate)}
        )

    async def submit(
        self, command: protocol.ExecutorJobCommand | Mapping[str, Any]
    ) -> ExecutorJobStatus:
        try:
            body = protocol.ExecutorJobCommand.model_validate(command)
        except (TypeError, ValueError, ValidationError) as exc:
            raise RemoteExecutorProtocolError("invalid_executor_command") from exc
        if body.job_id != deterministic_job_id(body.action_id, body.epoch):
            raise RemoteExecutorProtocolError("invalid_deterministic_job_id")
        binding = ExecutorJobBinding.from_command(body)
        token = self._token(binding, action="executor.submit", command_id=body.job_id)
        response = await self._request(
            "POST",
            "/v1/jobs",
            token=token,
            json_body=body.model_dump(mode="json"),
            side_effect=True,
        )
        if response.status_code == 409:
            self._raise_conflict(
                response, binding=binding, operation_id=binding.job_id
            )
        self._require_status(response, {202})
        status = self._decode(response, ExecutorJobStatus)
        self._validate_status(status, binding)
        self._verify_submit_receipt(status, binding)
        return status

    async def get_status(self, binding: ExecutorJobBinding) -> ExecutorJobStatus:
        token = self._token(
            binding,
            action="executor.status",
            command_id=f"{binding.job_id}:status",
        )
        response = await self._request(
            "GET",
            f"/v1/jobs/{binding.job_id}",
            token=token,
            side_effect=False,
        )
        self._require_status(response, {200})
        status = self._decode(response, ExecutorJobStatus)
        self._validate_status(status, binding)
        self._verify_submit_receipt(status, binding)
        return status

    async def get_result(
        self, binding: ExecutorJobBinding
    ) -> ExecutorJobResultEnvelope:
        token = self._token(
            binding,
            action="executor.result",
            command_id=f"{binding.job_id}:result.read",
        )
        response = await self._request(
            "GET",
            f"/v1/jobs/{binding.job_id}/result",
            token=token,
            side_effect=False,
        )
        self._require_status(response, {200})
        envelope = self._decode(response, ExecutorJobResultEnvelope)
        result = envelope.result
        if (
            result.job_id != binding.job_id
            or result.action_id != binding.action_id
            or result.epoch != binding.epoch
        ):
            raise RemoteExecutorProtocolError("result_binding_mismatch")
        self._validate_artifact_receipts(result, binding)
        receipt = self.receipt_verifier.verify(
            envelope.receipt,
            operation_id=f"{binding.job_id}:result",
            request_digest=binding.request_digest,
            run_id=binding.run_id,
            session_id=binding.session_id,
            action_id=binding.action_id,
            epoch=binding.epoch,
            status=result.state,
        )
        expected = {
            "job_id": binding.job_id,
            "state": result.state,
            "result_digest": result.result_digest,
            "teardown_proof_digest": protocol.content_digest(
                result.teardown_proof
            ),
        }
        if any(receipt.payload.get(key) != value for key, value in expected.items()):
            raise RemoteExecutorProtocolError("result_receipt_payload_mismatch")
        if (
            result.state != "reconciliation_required"
            and result.teardown_proof.get("removed") is not True
        ):
            raise RemoteExecutorProtocolError("terminal_result_without_teardown")
        return envelope

    async def wait_result(
        self,
        command: protocol.ExecutorJobCommand | Mapping[str, Any],
    ) -> ExecutorJobResultEnvelope:
        body = protocol.ExecutorJobCommand.model_validate(command)
        binding = ExecutorJobBinding.from_command(body)
        status: ExecutorJobStatus | None = None
        while status is None:
            try:
                status = await self.submit(body)
            except RemoteExecutorUncertainOutcome:
                try:
                    status = await self.get_status(binding)
                except RemoteExecutorProtocolError as exc:
                    if str(exc) != "executor_http_404":
                        raise
                except RemoteExecutorError:
                    pass
            if status is None:
                if datetime.now(UTC) >= body.deadline_at:
                    raise RemoteExecutorUncertainOutcome(
                        "executor submit remained uncertain; query the same job"
                    )
                await asyncio.sleep(self.poll_interval)
        while status.state not in EXECUTOR_TERMINAL_STATES:
            if datetime.now(UTC) >= body.deadline_at:
                raise RemoteExecutorUncertainOutcome(
                    "executor job deadline elapsed; query the same job"
                )
            await asyncio.sleep(self.poll_interval)
            try:
                status = await self.get_status(binding)
            except RemoteExecutorProtocolError:
                raise
            except RemoteExecutorError:
                continue
        while True:
            try:
                envelope = await self.get_result(binding)
                break
            except RemoteExecutorProtocolError:
                raise
            except RemoteExecutorError:
                if datetime.now(UTC) >= body.deadline_at:
                    raise RemoteExecutorUncertainOutcome(
                        "executor result read remained uncertain"
                    )
                await asyncio.sleep(self.poll_interval)
        self.validate_declared_outputs(envelope.result, body)
        if envelope.result.state == "reconciliation_required":
            raise RemoteExecutorUncertainOutcome(
                "executor reported reconciliation_required"
            )
        return envelope

    async def control(
        self,
        job_id: str,
        command: protocol.ControlCommand | Mapping[str, Any],
    ) -> protocol.ServiceReceipt:
        try:
            body = protocol.ControlCommand.model_validate(command)
        except (TypeError, ValueError, ValidationError) as exc:
            raise RemoteExecutorProtocolError("invalid_executor_control") from exc
        if body.target_kind != "executor":
            raise RemoteExecutorProtocolError("executor_control_target_mismatch")
        request_digest = canonical_request_digest(body)
        binding = ExecutorJobBinding(
            job_id=job_id,
            run_id=body.run_id,
            session_id=body.session_id,
            action_id=body.target_id,
            epoch=body.epoch,
            command_digest="0" * 64,
            request_digest=request_digest,
        )
        token = self._token(
            binding,
            action="executor.control",
            command_id=body.command_id,
        )
        response = await self._request(
            "POST",
            f"/v1/jobs/{job_id}/control",
            token=token,
            json_body=body.model_dump(mode="json"),
            side_effect=True,
        )
        if response.status_code == 409:
            self._raise_conflict(
                response, binding=binding, operation_id=body.command_id
            )
        self._require_status(response, {200})
        receipt = self._decode(response, protocol.ServiceReceipt)
        receipt = self.receipt_verifier.verify(
            receipt,
            operation_id=body.command_id,
            request_digest=request_digest,
            run_id=body.run_id,
            session_id=body.session_id,
            action_id=body.target_id,
            epoch=body.epoch,
        )
        payload = receipt.payload
        if (
            payload.get("job_id") != job_id
            or payload.get("request_id") != body.request_id
            or payload.get("target_id") != body.target_id
            or payload.get("action") != body.action
        ):
            raise RemoteExecutorProtocolError("control_receipt_payload_mismatch")
        if receipt.status == "confirmed_stopped":
            proof = payload.get("teardown_proof")
            if not isinstance(proof, dict) or proof.get("removed") is not True:
                raise RemoteExecutorProtocolError("control_teardown_unverified")
        elif receipt.status not in {"reconciliation_required", "fenced"}:
            raise RemoteExecutorProtocolError("invalid_control_receipt_status")
        return receipt

    def _token(
        self,
        binding: ExecutorJobBinding,
        *,
        action: str,
        command_id: str,
    ) -> str:
        return self.token_issuer.issue(
            run_id=binding.run_id,
            session_id=binding.session_id,
            epoch=binding.epoch,
            action=action,
            command_id=command_id,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        json_body: dict[str, Any] | None = None,
        side_effect: bool,
    ) -> httpx.Response:
        try:
            response = await self.http_client.request(
                method,
                f"{self.base_url}{path}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    **(
                        {"Content-Type": "application/json"}
                        if json_body is not None
                        else {}
                    ),
                },
                json=json_body,
                timeout=self.timeout,
            )
            if side_effect and response.status_code >= 500:
                raise RemoteExecutorUncertainOutcome(
                    f"remote Executor returned HTTP {response.status_code}"
                )
            return response
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            error_type = (
                RemoteExecutorUncertainOutcome if side_effect else RemoteExecutorError
            )
            raise error_type("remote Executor transport failure") from exc

    @staticmethod
    def _decode(response: httpx.Response, model_type):
        if len(response.content) > protocol.MAX_COMMAND_BYTES:
            raise RemoteExecutorProtocolError("executor_response_too_large")
        try:
            value = response.json()
            _enforce_depth(value)
            return model_type.model_validate(value)
        except (ValueError, ValidationError, UnicodeError, RecursionError) as exc:
            raise RemoteExecutorProtocolError("invalid_executor_response") from exc

    @staticmethod
    def _require_status(response: httpx.Response, allowed: set[int]) -> None:
        if response.status_code in allowed:
            return
        if response.status_code in {401, 403, 404, 413, 422}:
            raise RemoteExecutorProtocolError(
                f"executor_http_{response.status_code}"
            )
        if response.status_code >= 500:
            raise RemoteExecutorError(f"executor_http_{response.status_code}")
        raise RemoteExecutorProtocolError(f"executor_http_{response.status_code}")

    def _raise_conflict(
        self,
        response: httpx.Response,
        *,
        binding: ExecutorJobBinding,
        operation_id: str,
    ) -> None:
        try:
            value = response.json()
            receipt = protocol.ServiceReceipt.model_validate(value)
            verified = self.receipt_verifier.verify(
                receipt,
                operation_id=operation_id,
                request_digest=binding.request_digest,
                run_id=binding.run_id,
                session_id=binding.session_id,
                action_id=binding.action_id,
                epoch=binding.epoch,
            )
            if verified.status == "fenced":
                raise RemoteExecutorFenced("executor command was fenced")
        except RemoteExecutorFenced:
            raise
        except (ValueError, ValidationError, ReceiptValidationError, json.JSONDecodeError):
            pass
        raise RemoteExecutorConflict("executor command digest conflict")

    @staticmethod
    def _validate_status(
        status: ExecutorJobStatus, binding: ExecutorJobBinding
    ) -> None:
        if (
            status.job_id != binding.job_id
            or status.run_id != binding.run_id
            or status.session_id != binding.session_id
            or status.action_id != binding.action_id
            or status.epoch != binding.epoch
            or status.command_digest != binding.command_digest
            or status.request_digest != binding.request_digest
        ):
            raise RemoteExecutorProtocolError("executor_status_binding_mismatch")

    def _verify_submit_receipt(
        self,
        status: ExecutorJobStatus,
        binding: ExecutorJobBinding,
    ) -> None:
        receipt = self.receipt_verifier.verify(
            status.submit_receipt,
            operation_id=binding.job_id,
            request_digest=binding.request_digest,
            run_id=binding.run_id,
            session_id=binding.session_id,
            action_id=binding.action_id,
            epoch=binding.epoch,
            status="accepted",
        )
        expected_payload = {
            "job_id": binding.job_id,
            "state": "accepted",
            "command_digest": binding.command_digest,
            "instance_id": status.instance_id,
            "container_name": status.container_name,
        }
        if receipt.payload != expected_payload:
            raise RemoteExecutorProtocolError("submit_receipt_payload_mismatch")

    @staticmethod
    def _validate_artifact_receipts(
        result: protocol.ExecutorJobResult,
        binding: ExecutorJobBinding,
    ) -> dict[str, ExecutorArtifactEnvelope]:
        artifacts: dict[str, ExecutorArtifactEnvelope] = {}
        upload_ids: set[str] = set()
        try:
            envelopes = [
                ExecutorArtifactEnvelope.model_validate(value)
                for value in result.artifact_receipts
            ]
        except (TypeError, ValueError, ValidationError) as exc:
            raise RemoteExecutorProtocolError(
                "invalid_executor_artifact_receipt"
            ) from exc
        for envelope in envelopes:
            manifest = envelope.manifest
            receipt = envelope.upload_receipt
            if (
                manifest.artifact_id in artifacts
                or manifest.upload_id in upload_ids
                or manifest.producer_action_id != binding.action_id
                or receipt.action != "artifact.upload"
                or receipt.run_id != binding.run_id
                or receipt.session_id != binding.session_id
                or receipt.producer_action_id != binding.action_id
                or receipt.epoch != binding.epoch
            ):
                raise RemoteExecutorProtocolError(
                    "executor_artifact_receipt_binding_mismatch"
                )
            artifacts[manifest.artifact_id] = envelope
            upload_ids.add(manifest.upload_id)
        return artifacts

    @classmethod
    def _validate_declared_outputs(
        cls,
        result: protocol.ExecutorJobResult,
        outputs: list[protocol.ExecutorOutputSpec],
        binding: ExecutorJobBinding,
    ) -> dict[str, ExecutorArtifactEnvelope]:
        artifacts = cls._validate_artifact_receipts(result, binding)
        expected = {output.artifact_id: output for output in outputs}
        required = {
            output.artifact_id for output in outputs if output.required
        }
        if result.state == "succeeded" and not required.issubset(artifacts):
            raise RemoteExecutorProtocolError(
                "executor_declared_output_set_mismatch"
            )
        if not set(artifacts).issubset(expected):
            raise RemoteExecutorProtocolError(
                "executor_returned_undeclared_output"
            )
        for artifact_id, envelope in artifacts.items():
            spec = expected[artifact_id]
            manifest = envelope.manifest
            receipt = envelope.upload_receipt
            if (
                manifest.kind != spec.kind
                or manifest.expected_use != spec.expected_use
                or manifest.title != spec.title
                or manifest.content_type != spec.content_type
                or manifest.original_filename != spec.original_filename
                or manifest.required is not spec.required
                or manifest.byte_size > spec.max_bytes
                or manifest.upload_id != spec.lease.upload_id
                or receipt.tenant_id != spec.lease.tenant_id
                or (
                    spec.lease.expected_sha256 is not None
                    and manifest.sha256 != spec.lease.expected_sha256
                )
            ):
                raise RemoteExecutorProtocolError(
                    "executor_declared_output_binding_mismatch"
                )
        return artifacts

    @classmethod
    def validate_declared_outputs(
        cls,
        result: protocol.ExecutorJobResult | Mapping[str, Any],
        command: protocol.ExecutorJobCommand | Mapping[str, Any],
    ) -> dict[str, ExecutorArtifactEnvelope]:
        """Validate output evidence against the original canonical command."""
        try:
            body = protocol.ExecutorJobCommand.model_validate(command)
            result_body = protocol.ExecutorJobResult.model_validate(result)
        except (TypeError, ValueError, ValidationError) as exc:
            raise RemoteExecutorProtocolError(
                "invalid_executor_output_binding"
            ) from exc
        binding = ExecutorJobBinding.from_command(body)
        if (
            result_body.job_id != binding.job_id
            or result_body.action_id != binding.action_id
            or result_body.epoch != binding.epoch
        ):
            raise RemoteExecutorProtocolError("result_binding_mismatch")
        return cls._validate_declared_outputs(
            result_body, body.outputs, binding
        )


class HttpExecutorController:
    """Control-plane adapter rebuilt exclusively from a persisted job locator."""

    def __init__(
        self,
        *,
        token_issuer: ServiceTokenIssuer,
        receipt_verifier: ReceiptVerifier,
        timeout: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.token_issuer = token_issuer
        self.receipt_verifier = receipt_verifier
        self.timeout = timeout
        self.http_client = http_client

    async def __call__(self, command: Mapping[str, Any]) -> dict[str, Any]:
        if command.get("target_kind") != "executor":
            raise RemoteExecutorProtocolError(
                "executor controller received a non-executor target"
            )
        locator = command.get("locator")
        if not isinstance(locator, Mapping):
            raise RemoteExecutorProtocolError("executor locator is missing")
        required = {
            "base_url": locator.get("base_url"),
            "job_id": locator.get("job_id"),
            "run_id": locator.get("run_id"),
            "session_id": locator.get("session_id"),
            "action_id": locator.get("action_id"),
            "epoch": locator.get("epoch"),
        }
        if any(value is None or value == "" for value in required.values()):
            raise RemoteExecutorProtocolError("executor locator is incomplete")
        if any(
            not isinstance(required[name], str)
            for name in ("base_url", "job_id", "run_id", "session_id", "action_id")
        ) or type(required["epoch"]) is not int:
            raise RemoteExecutorProtocolError("executor locator has invalid types")
        if (
            required["run_id"] != command.get("run_id")
            or required["action_id"] != command.get("target_id")
            or required["epoch"] != command.get("epoch")
        ):
            raise RemoteExecutorProtocolError("executor locator binding mismatch")
        if required["job_id"] != deterministic_job_id(
            str(required["action_id"]), int(required["epoch"])
        ):
            raise RemoteExecutorProtocolError("executor locator job id mismatch")
        control_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                ":".join(
                    str(command[name])
                    for name in (
                        "request_id",
                        "run_id",
                        "target_kind",
                        "target_id",
                        "action",
                        "epoch",
                    )
                )
                + f":{required['job_id']}",
            )
        )
        body = protocol.ControlCommand.model_validate(
            {
                "schema_version": 2,
                "command_id": control_id,
                "request_id": command["request_id"],
                "run_id": command["run_id"],
                "session_id": required["session_id"],
                "target_kind": "executor",
                "target_id": command["target_id"],
                "action": command["action"],
                "epoch": command["epoch"],
                "deadline_at": command["deadline_at"],
            }
        )
        client = RemoteExecutorClient(
            base_url=str(required["base_url"]),
            token_issuer=self.token_issuer,
            receipt_verifier=self.receipt_verifier,
            timeout=self.timeout,
            http_client=self.http_client,
        )
        receipt = await client.control(str(required["job_id"]), body)
        return {
            **receipt.model_dump(mode="json"),
            "request_id": body.request_id,
            "target_id": body.target_id,
            "action": body.action,
            "job_state": receipt.payload.get("job_state"),
            "teardown_proof": receipt.payload.get("teardown_proof"),
        }


def executor_job_locator(
    *,
    base_url: str,
    command: protocol.ExecutorJobCommand,
    instance_id: str | None = None,
) -> dict[str, Any]:
    """Canonical locator to persist before the first submit attempt."""
    normalized_url = (base_url or "").rstrip("/")
    if not normalized_url:
        raise ValueError("executor locator base_url is required")
    if command.job_id != deterministic_job_id(command.action_id, command.epoch):
        raise ValueError("executor locator requires a deterministic job id")
    locator = {
        "provider": "lab-executor",
        "base_url": normalized_url,
        "job_id": command.job_id,
        "run_id": command.run_id,
        "session_id": command.session_id,
        "action_id": command.action_id,
        "epoch": command.epoch,
        "command_digest": command.command_digest,
        "request_digest": canonical_request_digest(command),
        "command": command.model_dump(mode="json"),
    }
    if instance_id:
        locator["instance_id"] = instance_id
    return locator


def configured_executor_auth() -> tuple[ServiceTokenIssuer, ReceiptVerifier]:
    """Build the two independent Runner-side Executor trust boundaries."""
    from app.config import settings

    auth_values = (
        settings.lab_executor_auth_issuer,
        settings.lab_executor_auth_current_kid,
        settings.lab_executor_auth_current_key,
        settings.lab_executor_auth_next_kid,
        settings.lab_executor_auth_next_key,
    )
    receipt_values = (
        settings.lab_executor_receipt_issuer,
        settings.lab_executor_receipt_current_kid,
        settings.lab_executor_receipt_current_key,
        settings.lab_executor_receipt_next_kid,
        settings.lab_executor_receipt_next_key,
    )
    if any(not value for value in auth_values):
        raise ValueError("remote Executor service-auth keyring is incomplete")
    if any(not value for value in receipt_values):
        raise ValueError("remote Executor receipt keyring is incomplete")
    if (
        settings.lab_executor_auth_current_kid
        == settings.lab_executor_auth_next_kid
        or settings.lab_executor_auth_current_key
        == settings.lab_executor_auth_next_key
    ):
        raise ValueError("remote Executor service-auth keys must be distinct")
    if (
        settings.lab_executor_receipt_current_kid
        == settings.lab_executor_receipt_next_kid
        or settings.lab_executor_receipt_current_key
        == settings.lab_executor_receipt_next_key
    ):
        raise ValueError("remote Executor receipt keys must be distinct")
    issuer = ServiceTokenIssuer(
        ServiceTokenIssuerConfig(
            issuer=settings.lab_executor_auth_issuer,
            audience=settings.lab_executor_auth_audience,
            current_kid=settings.lab_executor_auth_current_kid,
            current_key=settings.lab_executor_auth_current_key,
            token_ttl_seconds=settings.lab_executor_auth_token_ttl_s,
        )
    )
    verifier = ReceiptVerifier(
        ReceiptVerifierConfig(
            issuer=settings.lab_executor_receipt_issuer,
            audience=settings.lab_executor_receipt_audience,
            keys={
                settings.lab_executor_receipt_current_kid:
                    settings.lab_executor_receipt_current_key,
                settings.lab_executor_receipt_next_kid:
                    settings.lab_executor_receipt_next_key,
            },
        )
    )
    return issuer, verifier


def configured_remote_executor() -> RemoteExecutorClient:
    """Construct a fail-closed client from the Runner's dedicated settings."""
    from app.config import settings

    if not settings.lab_executor_enabled:
        raise ValueError("remote Executor is disabled")
    if not settings.lab_executor_base_url:
        raise ValueError("remote Executor base URL is required")
    if not settings.lab_executor_image_digest:
        raise ValueError("remote Executor image digest is required")
    issuer, verifier = configured_executor_auth()
    return RemoteExecutorClient(
        base_url=settings.lab_executor_base_url,
        token_issuer=issuer,
        receipt_verifier=verifier,
        timeout=settings.lab_executor_request_timeout_s,
        poll_interval=settings.lab_executor_poll_interval_s,
    )


def configured_executor_controller() -> HttpExecutorController:
    from app.config import settings

    issuer, verifier = configured_executor_auth()
    return HttpExecutorController(
        token_issuer=issuer,
        receipt_verifier=verifier,
        timeout=settings.lab_executor_request_timeout_s,
    )


def _enforce_depth(value: Any) -> None:
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if depth > 32:
            raise ValueError("executor response exceeds JSON depth cap")
        if isinstance(item, dict):
            stack.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            stack.extend((child, depth + 1) for child in item)


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )
