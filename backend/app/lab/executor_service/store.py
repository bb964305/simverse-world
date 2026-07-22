"""Executor-local durable job and control store backed by SQLite/WAL."""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import AsyncIterator, Iterable

import aiosqlite

from app.lab.protocol import (
    ControlCommand,
    ExecutorJobCommand,
    ExecutorJobResult,
    ExecutorOutputSpec,
    ServiceReceipt,
    content_digest,
)
from app.lab.artifact_services.canonical import canonical_digest
from app.lab.artifact_services.mime import declared_mime_matches
from app.lab.artifact_services.schemas import UploadReceipt
from app.lab.runtime_ref.service_auth import canonical_json_bytes

from .schemas import EXECUTOR_JOB_STATES, EXECUTOR_TERMINAL_STATES


STORE_VERSION = 1


class ExecutorStoreError(RuntimeError):
    pass


class ExecutorStoreConflict(ExecutorStoreError):
    pass


class ExecutorStoreNotFound(ExecutorStoreError):
    pass


class ExecutorStoreFenced(ExecutorStoreConflict):
    def __init__(self, highest_epoch: int) -> None:
        super().__init__(f"executor action is fenced at epoch {highest_epoch}")
        self.highest_epoch = highest_epoch


class ExecutorStoreBindingError(ExecutorStoreError):
    pass


class ExecutorStoreCapacity(ExecutorStoreError):
    pass


@dataclass(frozen=True)
class StoredJob:
    command: ExecutorJobCommand
    request_digest: str
    state: str
    instance_id: str
    container_name: str
    submit_receipt: ServiceReceipt
    result: ExecutorJobResult | None
    result_receipt: ServiceReceipt | None
    teardown_proof: dict | None
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class ControlClaim:
    job: StoredJob
    is_retry: bool
    receipt: ServiceReceipt | None


@dataclass(frozen=True)
class ArtifactUploadStage:
    spec: ExecutorOutputSpec
    spool_relpath: str
    byte_size: int
    sha256: str


@dataclass(frozen=True)
class StoredArtifactUpload:
    job_id: str
    spec: ExecutorOutputSpec
    spool_relpath: str
    state: str
    byte_size: int
    sha256: str
    receipt: UploadReceipt | None
    receipt_digest: str | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class StoredStagedExecution:
    job_id: str
    execution_digest: str
    state: str
    exit_code: int | None
    stdout: str
    stderr: str
    teardown_proof: dict
    error_code: str | None
    snapshot_relpath: str | None
    uploads: tuple[StoredArtifactUpload, ...]
    created_at: datetime


_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS executor_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS executor_action_fences (
        action_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        highest_epoch INTEGER NOT NULL CHECK (highest_epoch >= 0),
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS executor_jobs (
        job_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        action_id TEXT NOT NULL,
        epoch INTEGER NOT NULL CHECK (epoch >= 0),
        command_json TEXT NOT NULL,
        command_digest TEXT NOT NULL CHECK (length(command_digest) = 64),
        request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
        state TEXT NOT NULL CHECK (state IN (
            'accepted','starting','running','teardown_pending','succeeded','failed',
            'cancelling','terminating','killing','cancelled','terminated','killed',
            'reconciliation_required'
        )),
        instance_id TEXT NOT NULL,
        container_name TEXT NOT NULL UNIQUE,
        submit_receipt_json TEXT NOT NULL,
        result_json TEXT,
        result_receipt_json TEXT,
        teardown_json TEXT,
        error_code TEXT,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        updated_at TEXT NOT NULL,
        UNIQUE(action_id, epoch)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_executor_jobs_state
    ON executor_jobs(state)
    """,
    """
    CREATE TABLE IF NOT EXISTS executor_controls (
        command_id TEXT PRIMARY KEY,
        job_id TEXT NOT NULL REFERENCES executor_jobs(job_id),
        request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
        action TEXT NOT NULL CHECK (action IN ('cancel','terminate','kill')),
        epoch INTEGER NOT NULL CHECK (epoch >= 0),
        state TEXT NOT NULL CHECK (state IN ('accepted','completed')),
        receipt_json TEXT,
        created_at TEXT NOT NULL,
        completed_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS executor_staged_results (
        job_id TEXT PRIMARY KEY REFERENCES executor_jobs(job_id),
        execution_digest TEXT NOT NULL CHECK (length(execution_digest) = 64),
        state TEXT NOT NULL CHECK (state IN ('succeeded','failed')),
        exit_code INTEGER,
        stdout TEXT NOT NULL,
        stderr TEXT NOT NULL,
        teardown_json TEXT NOT NULL,
        error_code TEXT,
        snapshot_relpath TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS executor_artifact_uploads (
        job_id TEXT NOT NULL REFERENCES executor_jobs(job_id),
        artifact_id TEXT NOT NULL,
        upload_id TEXT NOT NULL UNIQUE,
        spec_json TEXT NOT NULL,
        spool_relpath TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending','uploading','completed','failed')),
        byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
        sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
        receipt_json TEXT,
        receipt_digest TEXT CHECK (receipt_digest IS NULL OR length(receipt_digest) = 64),
        error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY(job_id, artifact_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_executor_artifact_uploads_state
    ON executor_artifact_uploads(job_id, state)
    """,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _parse_timestamp(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _dump(value) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _load(value: str | None):
    return None if value is None else json.loads(value)


class ExecutorStore:
    def __init__(self, path: str | Path) -> None:
        raw_path = str(path)
        if not raw_path or raw_path == ":memory:" or "mode=memory" in raw_path:
            raise ValueError("executor store path must be a durable file")
        self.path = str(Path(raw_path).expanduser().resolve())
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    def _harden_files(self) -> None:
        if os.name != "posix":
            return
        for candidate in (self.path, f"{self.path}-wal", f"{self.path}-shm"):
            try:
                os.chmod(candidate, 0o600)
            except FileNotFoundError:
                pass

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            if os.name == "posix":
                descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
                os.close(descriptor)
            db = await aiosqlite.connect(self.path, isolation_level=None)
            try:
                await db.execute("PRAGMA foreign_keys = ON")
                await db.execute("PRAGMA journal_mode = WAL")
                await db.execute("PRAGMA busy_timeout = 5000")
                await db.execute("BEGIN IMMEDIATE")
                version = (await (await db.execute("PRAGMA user_version")).fetchone())[0]
                if version not in {0, STORE_VERSION}:
                    raise ExecutorStoreError(
                        f"unsupported executor store version {version}"
                    )
                for statement in _SCHEMA:
                    await db.execute(statement)
                await db.execute(f"PRAGMA user_version = {STORE_VERSION}")
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            finally:
                await db.close()
                self._harden_files()
            self._initialized = True

    async def _connect(self) -> aiosqlite.Connection:
        await self.initialize()
        db = await aiosqlite.connect(self.path, isolation_level=None)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA busy_timeout = 5000")
        return db

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            yield db
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()
            self._harden_files()

    @staticmethod
    def _job(row: aiosqlite.Row) -> StoredJob:
        return StoredJob(
            command=ExecutorJobCommand.model_validate(_load(row["command_json"])),
            request_digest=row["request_digest"],
            state=row["state"],
            instance_id=row["instance_id"],
            container_name=row["container_name"],
            submit_receipt=ServiceReceipt.model_validate(
                _load(row["submit_receipt_json"])
            ),
            result=(
                None
                if row["result_json"] is None
                else ExecutorJobResult.model_validate(_load(row["result_json"]))
            ),
            result_receipt=(
                None
                if row["result_receipt_json"] is None
                else ServiceReceipt.model_validate(_load(row["result_receipt_json"]))
            ),
            teardown_proof=_load(row["teardown_json"]),
            error_code=row["error_code"],
            created_at=_parse_timestamp(row["created_at"]),
            started_at=_parse_timestamp(row["started_at"]),
            finished_at=_parse_timestamp(row["finished_at"]),
        )

    @staticmethod
    def _artifact_upload(row: aiosqlite.Row) -> StoredArtifactUpload:
        return StoredArtifactUpload(
            job_id=row["job_id"],
            spec=ExecutorOutputSpec.model_validate(_load(row["spec_json"])),
            spool_relpath=row["spool_relpath"],
            state=row["state"],
            byte_size=row["byte_size"],
            sha256=row["sha256"],
            receipt=(
                None
                if row["receipt_json"] is None
                else UploadReceipt.model_validate(_load(row["receipt_json"]))
            ),
            receipt_digest=row["receipt_digest"],
            error_code=row["error_code"],
            created_at=_parse_timestamp(row["created_at"]),
            updated_at=_parse_timestamp(row["updated_at"]),
        )

    @classmethod
    async def _staged_execution(
        cls, db: aiosqlite.Connection, job_id: str
    ) -> StoredStagedExecution | None:
        row = await (
            await db.execute(
                "SELECT * FROM executor_staged_results WHERE job_id = ?", (job_id,)
            )
        ).fetchone()
        if row is None:
            return None
        upload_rows = await (
            await db.execute(
                "SELECT * FROM executor_artifact_uploads WHERE job_id = ? "
                "ORDER BY artifact_id",
                (job_id,),
            )
        ).fetchall()
        return StoredStagedExecution(
            job_id=job_id,
            execution_digest=row["execution_digest"],
            state=row["state"],
            exit_code=row["exit_code"],
            stdout=row["stdout"],
            stderr=row["stderr"],
            teardown_proof=_load(row["teardown_json"]),
            error_code=row["error_code"],
            snapshot_relpath=row["snapshot_relpath"],
            uploads=tuple(cls._artifact_upload(item) for item in upload_rows),
            created_at=_parse_timestamp(row["created_at"]),
        )

    async def ping(self) -> bool:
        try:
            db = await self._connect()
            try:
                row = await (await db.execute("SELECT 1")).fetchone()
                return row is not None and row[0] == 1
            finally:
                await db.close()
        except Exception:
            return False

    async def bind_identity(self, *, instance_id: str, image_digest: str) -> None:
        """Pin a durable store to one Executor host identity and OCI image."""
        expected = {"instance_id": instance_id, "image_digest": image_digest}
        async with self._transaction() as db:
            for key, value in expected.items():
                row = await (
                    await db.execute(
                        "SELECT value FROM executor_metadata WHERE key = ?", (key,)
                    )
                ).fetchone()
                if row is None:
                    await db.execute(
                        "INSERT INTO executor_metadata (key, value) VALUES (?, ?)",
                        (key, value),
                    )
                elif row["value"] != value:
                    raise ExecutorStoreConflict(
                        f"executor store {key} binding does not match this process"
                    )

    async def get_job(self, job_id: str) -> StoredJob | None:
        db = await self._connect()
        try:
            row = await (
                await db.execute(
                    "SELECT * FROM executor_jobs WHERE job_id = ?", (job_id,)
                )
            ).fetchone()
            return None if row is None else self._job(row)
        finally:
            await db.close()

    async def get_staged_execution(
        self, job_id: str
    ) -> StoredStagedExecution | None:
        db = await self._connect()
        try:
            return await self._staged_execution(db, job_id)
        finally:
            await db.close()

    async def stage_execution(
        self,
        job_id: str,
        *,
        state: str,
        exit_code: int | None,
        stdout: str,
        stderr: str,
        teardown_proof: dict,
        error_code: str | None,
        snapshot_relpath: str | None,
        uploads: tuple[ArtifactUploadStage, ...],
    ) -> StoredStagedExecution:
        if state not in {"succeeded", "failed"}:
            raise ValueError("staged executor result must be succeeded or failed")
        if teardown_proof.get("removed") is not True:
            raise ValueError("staged executor result requires verified teardown")
        if snapshot_relpath is not None:
            snapshot_parts = snapshot_relpath.split("/")
            if (
                snapshot_relpath.startswith("/")
                or "\\" in snapshot_relpath
                or len(snapshot_parts) != 1
                or any(part in {"", ".", ".."} for part in snapshot_parts)
            ):
                raise ValueError("staged output snapshot path is invalid")
        artifact_ids: set[str] = set()
        upload_ids: set[str] = set()
        for upload in uploads:
            spool_parts = upload.spool_relpath.split("/")
            if (
                snapshot_relpath is None
                or upload.spec.artifact_id in artifact_ids
                or upload.spec.lease.upload_id in upload_ids
                or upload.spool_relpath.startswith("/")
                or "\\" in upload.spool_relpath
                or any(part in {"", ".", ".."} for part in spool_parts)
                or len(spool_parts) < 2
                or spool_parts[0] != snapshot_relpath
                or upload.byte_size < 0
                or upload.byte_size > upload.spec.max_bytes
                or len(upload.sha256) != 64
                or any(char not in "0123456789abcdef" for char in upload.sha256)
                or (
                    upload.spec.lease.expected_sha256 is not None
                    and upload.spec.lease.expected_sha256 != upload.sha256
                )
            ):
                raise ValueError("staged artifact upload binding is invalid")
            artifact_ids.add(upload.spec.artifact_id)
            upload_ids.add(upload.spec.lease.upload_id)
        payload = {
            "job_id": job_id,
            "state": state,
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "teardown_proof": teardown_proof,
            "error_code": error_code,
            "snapshot_relpath": snapshot_relpath,
            "uploads": [
                {
                    "spec": upload.spec.model_dump(mode="json"),
                    "spool_relpath": upload.spool_relpath,
                    "byte_size": upload.byte_size,
                    "sha256": upload.sha256,
                }
                for upload in uploads
            ],
        }
        execution_digest = content_digest(payload)
        now = _timestamp()
        async with self._transaction() as db:
            job_row = await (
                await db.execute(
                    "SELECT * FROM executor_jobs WHERE job_id = ?", (job_id,)
                )
            ).fetchone()
            if job_row is None:
                raise ExecutorStoreNotFound("executor job not found")
            job = self._job(job_row)
            existing = await self._staged_execution(db, job_id)
            if existing is not None:
                if existing.execution_digest != execution_digest:
                    raise ExecutorStoreConflict("staged executor result changed")
                return existing
            if job.state != "teardown_pending":
                raise ExecutorStoreConflict(
                    "executor result can only be staged after teardown"
                )
            expected_outputs = {item.artifact_id: item for item in job.command.outputs}
            if state == "succeeded" and (
                set(expected_outputs) != artifact_ids
                or any(
                    expected_outputs[item.spec.artifact_id].model_dump(mode="json")
                    != item.spec.model_dump(mode="json")
                    for item in uploads
                )
            ):
                raise ExecutorStoreConflict(
                    "successful executor result is missing declared outputs"
                )
            await db.execute(
                "INSERT INTO executor_staged_results "
                "(job_id, execution_digest, state, exit_code, stdout, stderr, "
                "teardown_json, error_code, snapshot_relpath, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    execution_digest,
                    state,
                    exit_code,
                    stdout,
                    stderr,
                    _dump(teardown_proof),
                    error_code,
                    snapshot_relpath,
                    now,
                ),
            )
            for upload in uploads:
                await db.execute(
                    "INSERT INTO executor_artifact_uploads "
                    "(job_id, artifact_id, upload_id, spec_json, spool_relpath, "
                    "state, byte_size, sha256, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                    (
                        job_id,
                        upload.spec.artifact_id,
                        upload.spec.lease.upload_id,
                        _dump(upload.spec.model_dump(mode="json")),
                        upload.spool_relpath,
                        upload.byte_size,
                        upload.sha256,
                        now,
                        now,
                    ),
                )
            staged = await self._staged_execution(db, job_id)
            assert staged is not None
            return staged

    async def mark_artifact_uploading(
        self, job_id: str, artifact_id: str
    ) -> StoredArtifactUpload:
        async with self._transaction() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM executor_artifact_uploads "
                    "WHERE job_id = ? AND artifact_id = ?",
                    (job_id, artifact_id),
                )
            ).fetchone()
            if row is None:
                raise ExecutorStoreNotFound("executor artifact upload not found")
            upload = self._artifact_upload(row)
            if upload.state in {"completed", "failed", "uploading"}:
                return upload
            now = _timestamp()
            await db.execute(
                "UPDATE executor_artifact_uploads SET state = 'uploading', "
                "updated_at = ? WHERE job_id = ? AND artifact_id = ?",
                (now, job_id, artifact_id),
            )
            updated = await (
                await db.execute(
                    "SELECT * FROM executor_artifact_uploads "
                    "WHERE job_id = ? AND artifact_id = ?",
                    (job_id, artifact_id),
                )
            ).fetchone()
            return self._artifact_upload(updated)

    async def record_artifact_receipt(
        self,
        job_id: str,
        artifact_id: str,
        *,
        receipt: UploadReceipt,
    ) -> StoredArtifactUpload:
        async with self._transaction() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM executor_artifact_uploads "
                    "WHERE job_id = ? AND artifact_id = ?",
                    (job_id, artifact_id),
                )
            ).fetchone()
            if row is None:
                raise ExecutorStoreNotFound("executor artifact upload not found")
            upload = self._artifact_upload(row)
            receipt_digest = canonical_digest(receipt)
            if upload.receipt is not None:
                if upload.receipt_digest != receipt_digest:
                    raise ExecutorStoreConflict("executor artifact receipt changed")
                return upload
            spec = upload.spec
            if (
                receipt.upload_id != spec.lease.upload_id
                or receipt.artifact_id != spec.artifact_id
                or receipt.run_id != spec.lease.run_id
                or receipt.session_id != spec.lease.session_id
                or receipt.producer_action_id != spec.lease.producer_action_id
                or receipt.epoch != spec.lease.epoch
            ):
                raise ExecutorStoreConflict("executor artifact receipt binding mismatch")
            if receipt.status == "completed" and (
                receipt.byte_size != upload.byte_size
                or receipt.sha256 != upload.sha256
                or not declared_mime_matches(
                    spec.content_type, receipt.content_type or ""
                )
            ):
                raise ExecutorStoreConflict("executor artifact bytes changed at Ingest")
            state = "completed" if receipt.status == "completed" else "failed"
            now = _timestamp()
            await db.execute(
                "UPDATE executor_artifact_uploads SET state = ?, receipt_json = ?, "
                "receipt_digest = ?, error_code = ?, updated_at = ? "
                "WHERE job_id = ? AND artifact_id = ?",
                (
                    state,
                    _dump(receipt.model_dump(mode="json")),
                    receipt_digest,
                    receipt.error_code,
                    now,
                    job_id,
                    artifact_id,
                ),
            )
            updated = await (
                await db.execute(
                    "SELECT * FROM executor_artifact_uploads "
                    "WHERE job_id = ? AND artifact_id = ?",
                    (job_id, artifact_id),
                )
            ).fetchone()
            return self._artifact_upload(updated)

    async def record_artifact_upload_error(
        self,
        job_id: str,
        artifact_id: str,
        *,
        error_code: str,
    ) -> StoredArtifactUpload:
        if not error_code or len(error_code) > 100:
            raise ValueError("artifact upload error code is invalid")
        async with self._transaction() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM executor_artifact_uploads "
                    "WHERE job_id = ? AND artifact_id = ?",
                    (job_id, artifact_id),
                )
            ).fetchone()
            if row is None:
                raise ExecutorStoreNotFound("executor artifact upload not found")
            upload = self._artifact_upload(row)
            if upload.state in {"completed", "failed"}:
                return upload
            now = _timestamp()
            await db.execute(
                "UPDATE executor_artifact_uploads SET state = 'failed', "
                "error_code = ?, updated_at = ? "
                "WHERE job_id = ? AND artifact_id = ?",
                (error_code, now, job_id, artifact_id),
            )
            updated = await (
                await db.execute(
                    "SELECT * FROM executor_artifact_uploads "
                    "WHERE job_id = ? AND artifact_id = ?",
                    (job_id, artifact_id),
                )
            ).fetchone()
            return self._artifact_upload(updated)

    async def count_recoverable(self) -> int:
        placeholders = ",".join("?" for _ in EXECUTOR_TERMINAL_STATES)
        db = await self._connect()
        try:
            row = await (
                await db.execute(
                    f"SELECT count(*) FROM executor_jobs "
                    f"WHERE state NOT IN ({placeholders})",
                    tuple(sorted(EXECUTOR_TERMINAL_STATES)),
                )
            ).fetchone()
            return int(row[0])
        finally:
            await db.close()

    async def accept_job(
        self,
        command: ExecutorJobCommand,
        *,
        request_digest: str,
        instance_id: str,
        container_name: str,
        submit_receipt: ServiceReceipt,
        max_pending_jobs: int,
    ) -> tuple[StoredJob, bool]:
        command_json = _dump(command.model_dump(mode="json"))
        now = _timestamp()
        async with self._transaction() as db:
            existing = await (
                await db.execute(
                    "SELECT * FROM executor_jobs WHERE job_id = ?", (command.job_id,)
                )
            ).fetchone()
            if existing is not None:
                job = self._job(existing)
                if (
                    job.request_digest != request_digest
                    or job.command.command_digest != command.command_digest
                    or job.command.model_dump(mode="json")
                    != command.model_dump(mode="json")
                ):
                    raise ExecutorStoreConflict(
                        "job id was reused with a different command digest"
                    )
                return job, True

            placeholders = ",".join("?" for _ in EXECUTOR_TERMINAL_STATES)
            active_count = await (
                await db.execute(
                    f"SELECT count(*) FROM executor_jobs "
                    f"WHERE state NOT IN ({placeholders})",
                    tuple(sorted(EXECUTOR_TERMINAL_STATES)),
                )
            ).fetchone()
            if int(active_count[0]) >= max_pending_jobs:
                raise ExecutorStoreCapacity("executor capacity is exhausted")

            fence = await (
                await db.execute(
                    "SELECT * FROM executor_action_fences WHERE action_id = ?",
                    (command.action_id,),
                )
            ).fetchone()
            if fence is not None:
                if (
                    fence["run_id"] != command.run_id
                    or fence["session_id"] != command.session_id
                ):
                    raise ExecutorStoreConflict("action id was reused across bindings")
                if command.epoch < fence["highest_epoch"]:
                    raise ExecutorStoreFenced(fence["highest_epoch"])
                if command.epoch > fence["highest_epoch"]:
                    await db.execute(
                        "UPDATE executor_action_fences SET highest_epoch = ?, "
                        "updated_at = ? WHERE action_id = ?",
                        (command.epoch, now, command.action_id),
                    )
            else:
                await db.execute(
                    "INSERT INTO executor_action_fences "
                    "(action_id, run_id, session_id, highest_epoch, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        command.action_id,
                        command.run_id,
                        command.session_id,
                        command.epoch,
                        now,
                    ),
                )

            await db.execute(
                "INSERT INTO executor_jobs "
                "(job_id, run_id, session_id, action_id, epoch, command_json, "
                "command_digest, request_digest, state, instance_id, container_name, "
                "submit_receipt_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?, ?)",
                (
                    command.job_id,
                    command.run_id,
                    command.session_id,
                    command.action_id,
                    command.epoch,
                    command_json,
                    command.command_digest,
                    request_digest,
                    instance_id,
                    container_name,
                    _dump(submit_receipt.model_dump(mode="json")),
                    now,
                    now,
                ),
            )
            row = await (
                await db.execute(
                    "SELECT * FROM executor_jobs WHERE job_id = ?", (command.job_id,)
                )
            ).fetchone()
            return self._job(row), False

    async def transition_job(
        self,
        job_id: str,
        *,
        expected_states: Iterable[str],
        new_state: str,
        mark_started: bool = False,
        error_code: str | None = None,
    ) -> tuple[StoredJob, bool]:
        expected = set(expected_states)
        if not expected or not expected <= EXECUTOR_JOB_STATES:
            raise ValueError("unknown expected executor state")
        if new_state not in EXECUTOR_JOB_STATES:
            raise ValueError("unknown executor state")
        now = _timestamp()
        async with self._transaction() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM executor_jobs WHERE job_id = ?", (job_id,)
                )
            ).fetchone()
            if row is None:
                raise ExecutorStoreNotFound("executor job not found")
            if row["state"] not in expected:
                return self._job(row), False
            started_at = now if mark_started and row["started_at"] is None else row["started_at"]
            await db.execute(
                "UPDATE executor_jobs SET state = ?, started_at = ?, error_code = ?, "
                "updated_at = ? WHERE job_id = ?",
                (new_state, started_at, error_code, now, job_id),
            )
            updated = await (
                await db.execute(
                    "SELECT * FROM executor_jobs WHERE job_id = ?", (job_id,)
                )
            ).fetchone()
            return self._job(updated), True

    async def complete_job(
        self,
        job_id: str,
        *,
        expected_states: Iterable[str],
        result: ExecutorJobResult,
        receipt: ServiceReceipt,
        error_code: str | None = None,
    ) -> tuple[StoredJob, bool]:
        expected = set(expected_states)
        if not expected or not expected <= EXECUTOR_JOB_STATES:
            raise ValueError("unknown expected executor state")
        if result.state not in EXECUTOR_TERMINAL_STATES:
            raise ValueError("executor result must be terminal")
        now = _timestamp()
        async with self._transaction() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM executor_jobs WHERE job_id = ?", (job_id,)
                )
            ).fetchone()
            if row is None:
                raise ExecutorStoreNotFound("executor job not found")
            job = self._job(row)
            if (
                result.job_id != job.command.job_id
                or result.action_id != job.command.action_id
                or result.epoch != job.command.epoch
            ):
                raise ExecutorStoreConflict("executor result binding mismatch")
            if job.result is not None:
                if job.result.result_digest != result.result_digest:
                    raise ExecutorStoreConflict("executor result digest conflict")
                return job, False
            if job.state not in expected:
                return job, False
            await db.execute(
                "UPDATE executor_jobs SET state = ?, result_json = ?, "
                "result_receipt_json = ?, teardown_json = ?, error_code = ?, "
                "finished_at = ?, updated_at = ? WHERE job_id = ?",
                (
                    result.state,
                    _dump(result.model_dump(mode="json")),
                    _dump(receipt.model_dump(mode="json")),
                    _dump(result.teardown_proof),
                    error_code,
                    now,
                    now,
                    job_id,
                ),
            )
            updated = await (
                await db.execute(
                    "SELECT * FROM executor_jobs WHERE job_id = ?", (job_id,)
                )
            ).fetchone()
            return self._job(updated), True

    async def claim_control(
        self,
        job_id: str,
        command: ControlCommand,
        *,
        request_digest: str,
    ) -> ControlClaim:
        transitional = {
            "cancel": "cancelling",
            "terminate": "terminating",
            "kill": "killing",
        }[command.action]
        now = _timestamp()
        async with self._transaction() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM executor_jobs WHERE job_id = ?", (job_id,)
                )
            ).fetchone()
            if row is None:
                raise ExecutorStoreNotFound("executor job not found")
            job = self._job(row)
            if (
                command.target_kind != "executor"
                or command.target_id != job.command.action_id
                or command.run_id != job.command.run_id
                or command.session_id != job.command.session_id
            ):
                raise ExecutorStoreBindingError("executor control binding mismatch")
            if command.epoch != job.command.epoch:
                raise ExecutorStoreFenced(job.command.epoch)

            known = await (
                await db.execute(
                    "SELECT * FROM executor_controls WHERE command_id = ?",
                    (command.command_id,),
                )
            ).fetchone()
            if known is not None:
                if (
                    known["job_id"] != job_id
                    or known["request_digest"] != request_digest
                    or known["action"] != command.action
                    or known["epoch"] != command.epoch
                ):
                    raise ExecutorStoreConflict(
                        "control command id was reused across bindings"
                    )
                receipt = (
                    None
                    if known["receipt_json"] is None
                    else ServiceReceipt.model_validate(_load(known["receipt_json"]))
                )
                return ControlClaim(job=job, is_retry=True, receipt=receipt)

            await db.execute(
                "INSERT INTO executor_controls "
                "(command_id, job_id, request_digest, action, epoch, state, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'accepted', ?)",
                (
                    command.command_id,
                    job_id,
                    request_digest,
                    command.action,
                    command.epoch,
                    now,
                ),
            )
            if job.state not in EXECUTOR_TERMINAL_STATES:
                if job.state in {"cancelling", "terminating", "killing"}:
                    raise ExecutorStoreConflict("another control command is active")
                await db.execute(
                    "UPDATE executor_jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                    (transitional, now, job_id),
                )
                row = await (
                    await db.execute(
                        "SELECT * FROM executor_jobs WHERE job_id = ?", (job_id,)
                    )
                ).fetchone()
                job = self._job(row)
            return ControlClaim(job=job, is_retry=False, receipt=None)

    async def complete_control(
        self, command_id: str, *, receipt: ServiceReceipt
    ) -> ServiceReceipt:
        now = _timestamp()
        async with self._transaction() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM executor_controls WHERE command_id = ?",
                    (command_id,),
                )
            ).fetchone()
            if row is None:
                raise ExecutorStoreNotFound("executor control command not found")
            if row["state"] == "completed":
                known = ServiceReceipt.model_validate(_load(row["receipt_json"]))
                if known.payload_digest != receipt.payload_digest:
                    raise ExecutorStoreConflict("executor control receipt conflict")
                return known
            await db.execute(
                "UPDATE executor_controls SET state = 'completed', receipt_json = ?, "
                "completed_at = ? WHERE command_id = ?",
                (_dump(receipt.model_dump(mode="json")), now, command_id),
            )
            return receipt

    async def list_recoverable(self) -> list[StoredJob]:
        placeholders = ",".join("?" for _ in EXECUTOR_TERMINAL_STATES)
        db = await self._connect()
        try:
            rows = await (
                await db.execute(
                    f"SELECT * FROM executor_jobs WHERE state NOT IN ({placeholders}) "
                    "ORDER BY created_at",
                    tuple(sorted(EXECUTOR_TERMINAL_STATES)),
                )
            ).fetchall()
            return [self._job(row) for row in rows]
        finally:
            await db.close()

    async def list_lower_recoverable(
        self, *, action_id: str, epoch: int
    ) -> list[StoredJob]:
        placeholders = ",".join("?" for _ in EXECUTOR_TERMINAL_STATES)
        db = await self._connect()
        try:
            rows = await (
                await db.execute(
                    "SELECT * FROM executor_jobs WHERE action_id = ? AND epoch < ? "
                    f"AND state NOT IN ({placeholders}) ORDER BY epoch",
                    (action_id, epoch, *tuple(sorted(EXECUTOR_TERMINAL_STATES))),
                )
            ).fetchall()
            return [self._job(row) for row in rows]
        finally:
            await db.close()
