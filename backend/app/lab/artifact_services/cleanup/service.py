"""Version-pinned cleanup with durable per-target delete proofs."""
from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from app.lab.artifact_services.canonical import canonical_digest
from app.lab.artifact_services.receipts import ReceiptSigner
from app.lab.artifact_services.schemas import DeleteCommand, DeleteProof, DeleteReceipt
from app.lab.artifact_services.storage.base import ObjectStorage, StorageError
from app.lab.artifact_services.store import OperationConflict, OperationStore


class CleanupError(RuntimeError):
    status_code = 400

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class CleanupConflict(CleanupError):
    status_code = 409


@dataclass(frozen=True)
class CleanupConfig:
    service_instance_id: str
    claim_seconds: int = 120

    def __post_init__(self) -> None:
        if not self.service_instance_id or self.claim_seconds <= 0:
            raise ValueError("cleanup service configuration is invalid")


def _target_identity(proof: DeleteProof) -> str:
    ref = proof.object_ref
    return canonical_digest(
        {
            "backend": ref.backend,
            "bucket": ref.bucket,
            "key": ref.key,
            "version_id": ref.version_id,
        }
    )


class CleanupService:
    def __init__(
        self,
        *,
        config: CleanupConfig,
        store: OperationStore,
        storage: ObjectStorage,
        receipt_signer: ReceiptSigner,
    ) -> None:
        self.config = config
        self.store = store
        self.storage = storage
        self.receipt_signer = receipt_signer
        self.owner_id = f"{socket.gethostname()}:{os.getpid()}:{config.service_instance_id}"

    async def initialize(self) -> None:
        await self.store.initialize()

    async def ready(self) -> bool:
        return await self.store.ready() and await self.storage.ready()

    def _receipt(
        self,
        command: DeleteCommand,
        *,
        status: str,
        proofs: list[DeleteProof],
        error_code: str | None,
    ) -> DeleteReceipt:
        occurred = datetime.now(UTC)
        outcome = {
            "status": status,
            "proofs": [proof.model_dump(mode="json") for proof in proofs],
            "error_code": error_code,
        }
        return self.receipt_signer.sign(
            DeleteReceipt,
            {
                "receipt_type": "artifact.delete",
                "receipt_id": str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"simverse:artifact-delete:{command.delete_operation_id}:{status}",
                    )
                ),
                "service_instance_id": self.config.service_instance_id,
                "command_id": command.command_id,
                "action": "artifact.delete",
                "request_digest": canonical_digest(command),
                "tenant_id": command.tenant_id,
                "run_id": command.run_id,
                "session_id": command.session_id,
                "artifact_id": command.artifact_id,
                "producer_action_id": command.producer_action_id,
                "epoch": command.epoch,
                "status": status,
                "occurred_at": occurred,
                "payload_digest": canonical_digest(outcome),
                "delete_operation_id": command.delete_operation_id,
                "proofs": proofs,
                "completed_at": occurred,
                "error_code": error_code,
            },
        )

    async def delete(self, command: DeleteCommand) -> DeleteReceipt:
        claim_owner = f"{self.owner_id}:{uuid.uuid4()}"
        try:
            operation = await self.store.create_or_get(
                operation_id=command.delete_operation_id,
                kind="delete",
                command_digest=canonical_digest(command),
                command=command.model_dump(mode="json"),
                initial_state="pending",
            )
        except OperationConflict as exc:
            raise CleanupConflict("delete_operation_id_conflict") from exc
        if operation.record.state == "completed" and operation.record.response:
            return DeleteReceipt.model_validate(operation.record.response)
        claimed = await self.store.claim(
            command.delete_operation_id,
            owner=claim_owner,
            eligible_states=("pending", "deleting", "failed"),
            claimed_state="deleting",
            lease_seconds=self.config.claim_seconds,
        )
        if claimed is None:
            refreshed = await self.store.get(command.delete_operation_id)
            if refreshed and refreshed.state == "completed" and refreshed.response:
                return DeleteReceipt.model_validate(refreshed.response)
            raise CleanupConflict("delete_operation_in_progress")
        if command.deadline_at <= datetime.now(UTC):
            receipt = self._receipt(
                command, status="failed", proofs=[], error_code="delete_deadline_expired"
            )
            await self.store.set_response(
                command.delete_operation_id,
                state="failed",
                response=receipt.model_dump(mode="json"),
                error_code="delete_deadline_expired",
                expected_states=("deleting",),
                owner=claim_owner,
            )
            return receipt

        proofs = [
            DeleteProof.model_validate(value)
            for value in claimed.progress.get("proofs", [])
        ]
        proven = {_target_identity(proof) for proof in proofs}
        for target in command.targets:
            marker = canonical_digest(
                {
                    "backend": target.object_ref.backend,
                    "bucket": target.object_ref.bucket,
                    "key": target.object_ref.key,
                    "version_id": target.object_ref.version_id,
                }
            )
            if marker in proven:
                continue
            try:
                proof = await self.storage.delete_exact(target.object_ref)
            except StorageError:
                receipt = self._receipt(
                    command,
                    status="failed",
                    proofs=proofs,
                    error_code="exact_delete_unconfirmed",
                )
                await self.store.set_response(
                    command.delete_operation_id,
                    state="failed",
                    response=receipt.model_dump(mode="json"),
                    progress={
                        "proofs": [value.model_dump(mode="json") for value in proofs]
                    },
                    error_code="exact_delete_unconfirmed",
                    expected_states=("deleting",),
                    owner=claim_owner,
                )
                return receipt
            proofs.append(proof)
            proven.add(marker)
            await self.store.set_progress(
                command.delete_operation_id,
                state="deleting",
                progress={
                    "proofs": [value.model_dump(mode="json") for value in proofs]
                },
                owner=claim_owner,
            )

        receipt = self._receipt(command, status="completed", proofs=proofs, error_code=None)
        await self.store.set_response(
            command.delete_operation_id,
            state="completed",
            response=receipt.model_dump(mode="json"),
            progress={"proofs": [value.model_dump(mode="json") for value in proofs]},
            expected_states=("deleting",),
            owner=claim_owner,
        )
        return receipt

    async def get_receipt(self, delete_operation_id: str) -> DeleteReceipt:
        record = await self.store.get(delete_operation_id)
        if record is None or record.kind != "delete" or record.response is None:
            raise CleanupError("delete_operation_not_found")
        return DeleteReceipt.model_validate(record.response)
