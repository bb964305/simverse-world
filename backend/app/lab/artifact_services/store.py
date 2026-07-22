"""SQLite/WAL durable idempotency and work-claim store."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterable

import aiosqlite

from app.lab.artifact_services.canonical import canonical_json_bytes


class OperationStoreError(RuntimeError):
    pass


class OperationConflict(OperationStoreError):
    pass


class OperationNotFound(OperationStoreError):
    pass


@dataclass(frozen=True)
class OperationRecord:
    operation_id: str
    kind: str
    command_digest: str
    state: str
    command: dict
    response: dict | None
    progress: dict
    error_code: str | None
    claim_owner: str | None
    claim_expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class OperationClaim:
    record: OperationRecord
    is_retry: bool


_SCHEMA = """
CREATE TABLE IF NOT EXISTS artifact_operations (
    operation_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    command_digest TEXT NOT NULL,
    state TEXT NOT NULL,
    command_json TEXT NOT NULL,
    response_json TEXT,
    progress_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    claim_owner TEXT,
    claim_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_artifact_operations_work
    ON artifact_operations(kind, state, claim_expires_at, created_at);
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _dump(value: dict) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _dt(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _record(row: aiosqlite.Row) -> OperationRecord:
    return OperationRecord(
        operation_id=row["operation_id"],
        kind=row["kind"],
        command_digest=row["command_digest"],
        state=row["state"],
        command=json.loads(row["command_json"]),
        response=None if row["response_json"] is None else json.loads(row["response_json"]),
        progress=json.loads(row["progress_json"]),
        error_code=row["error_code"],
        claim_owner=row["claim_owner"],
        claim_expires_at=_dt(row["claim_expires_at"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


class OperationStore:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        if str(path) == ":memory:":
            raise ValueError("durable operation store cannot use :memory:")
        self.path = Path(path)

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=FULL")
            await db.executescript(_SCHEMA)
            await db.commit()
        os.chmod(self.path, 0o600)

    async def _connect(self) -> aiosqlite.Connection:
        db = await aiosqlite.connect(self.path, timeout=30)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=30000")
        return db

    async def ready(self) -> bool:
        try:
            db = await self._connect()
            try:
                await db.execute("SELECT operation_id FROM artifact_operations LIMIT 1")
            finally:
                await db.close()
            return True
        except Exception:
            return False

    async def get(self, operation_id: str) -> OperationRecord | None:
        db = await self._connect()
        try:
            row = await db.execute_fetchall(
                "SELECT * FROM artifact_operations WHERE operation_id = ?",
                (operation_id,),
            )
            return None if not row else _record(row[0])
        finally:
            await db.close()

    async def create_or_get(
        self,
        *,
        operation_id: str,
        kind: str,
        command_digest: str,
        command: dict,
        initial_state: str,
    ) -> OperationClaim:
        now = _now().isoformat()
        db = await self._connect()
        try:
            await db.execute("BEGIN IMMEDIATE")
            rows = await db.execute_fetchall(
                "SELECT * FROM artifact_operations WHERE operation_id = ?",
                (operation_id,),
            )
            if rows:
                existing = _record(rows[0])
                if existing.kind != kind or existing.command_digest != command_digest:
                    raise OperationConflict("operation ID was rebound to a different command")
                await db.commit()
                return OperationClaim(existing, True)
            await db.execute(
                "INSERT INTO artifact_operations "
                "(operation_id, kind, command_digest, state, command_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (operation_id, kind, command_digest, initial_state, _dump(command), now, now),
            )
            await db.commit()
            created = await self.get(operation_id)
            assert created is not None
            return OperationClaim(created, False)
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.close()

    async def set_response(
        self,
        operation_id: str,
        *,
        state: str,
        response: dict,
        progress: dict | None = None,
        error_code: str | None = None,
        expected_states: Iterable[str] | None = None,
        clear_claim: bool = True,
        owner: str | None = None,
    ) -> OperationRecord:
        expected = tuple(expected_states or ())
        clauses: list[str] = []
        params: list[object] = [
            state,
            _dump(response),
            _dump(progress or {}),
            error_code,
            _now().isoformat(),
        ]
        if clear_claim:
            claim_sql = ", claim_owner = NULL, claim_expires_at = NULL"
        else:
            claim_sql = ""
        if expected:
            clauses.append(
                "state IN ({})".format(",".join("?" for _ in expected))
            )
        params.append(operation_id)
        if expected:
            params.extend(expected)
        if owner is not None:
            clauses.append("claim_owner = ?")
            params.append(owner)
        where_suffix = "" if not clauses else " AND " + " AND ".join(clauses)
        db = await self._connect()
        try:
            result = await db.execute(
                "UPDATE artifact_operations SET state = ?, response_json = ?, progress_json = ?, "
                f"error_code = ?, updated_at = ?{claim_sql} "
                f"WHERE operation_id = ?{where_suffix}",
                tuple(params),
            )
            await db.commit()
            if result.rowcount != 1:
                raise OperationConflict("operation state changed concurrently")
            record = await self.get(operation_id)
            assert record is not None
            return record
        finally:
            await db.close()

    async def set_progress(
        self,
        operation_id: str,
        *,
        state: str,
        progress: dict,
        error_code: str | None = None,
        owner: str | None = None,
        clear_claim: bool = False,
    ) -> OperationRecord:
        db = await self._connect()
        try:
            statement = (
                "UPDATE artifact_operations SET state = ?, progress_json = ?, error_code = ?, "
                "updated_at = ?"
            )
            if clear_claim:
                statement += ", claim_owner = NULL, claim_expires_at = NULL"
            statement += " WHERE operation_id = ?"
            params: tuple[object, ...] = (
                state, _dump(progress), error_code, _now().isoformat(), operation_id,
            )
            if owner is not None:
                statement += " AND claim_owner = ?"
                params += (owner,)
            result = await db.execute(statement, params)
            await db.commit()
            if result.rowcount != 1:
                raise OperationConflict("operation progress claim was lost")
            record = await self.get(operation_id)
            assert record is not None
            return record
        finally:
            await db.close()

    async def claim(
        self,
        operation_id: str,
        *,
        owner: str,
        eligible_states: Iterable[str],
        claimed_state: str,
        lease_seconds: int,
    ) -> OperationRecord | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = _now()
        eligible = tuple(eligible_states)
        if not eligible:
            raise ValueError("eligible_states must not be empty")
        db = await self._connect()
        try:
            result = await db.execute(
                "UPDATE artifact_operations SET state = ?, claim_owner = ?, "
                "claim_expires_at = ?, updated_at = ? WHERE operation_id = ? "
                "AND state IN ({}) AND (claim_expires_at IS NULL OR claim_expires_at <= ?)"
                .format(",".join("?" for _ in eligible)),
                (
                    claimed_state,
                    owner,
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    now.isoformat(),
                    operation_id,
                    *eligible,
                    now.isoformat(),
                ),
            )
            await db.commit()
            if result.rowcount != 1:
                return None
            return await self.get(operation_id)
        finally:
            await db.close()

    async def list_runnable(
        self,
        *,
        kind: str,
        states: Iterable[str],
        limit: int = 100,
    ) -> list[str]:
        now = _now().isoformat()
        states = tuple(states)
        if not states:
            return []
        db = await self._connect()
        try:
            rows = await db.execute_fetchall(
                "SELECT operation_id FROM artifact_operations WHERE kind = ? "
                "AND state IN ({}) AND (claim_expires_at IS NULL OR claim_expires_at <= ?) "
                "ORDER BY created_at LIMIT ?".format(",".join("?" for _ in states)),
                (kind, *states, now, limit),
            )
            return [row["operation_id"] for row in rows]
        finally:
            await db.close()
