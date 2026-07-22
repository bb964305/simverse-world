"""Small durable action store for idempotent egress execution and recovery."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass

from app.lab.protocol import canonical_json

from .models import EgressActionCommand, EgressActionStatus, EgressUsage


class EgressStoreConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class EgressClaim:
    status: EgressActionStatus
    lease_token: str | None


class EgressActionStore:
    def __init__(
        self,
        path: str,
        *,
        lease_seconds: int,
        max_attempts: int,
    ) -> None:
        if not os.path.isabs(path):
            raise ValueError("egress store path must be absolute")
        parent = os.path.dirname(path)
        os.makedirs(parent, mode=0o700, exist_ok=True)
        self.path = path
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self._lock = asyncio.Lock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS egress_actions (
                    action_id TEXT PRIMARY KEY,
                    request_digest TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_json TEXT,
                    error_code TEXT,
                    usage_requests INTEGER NOT NULL DEFAULT 0,
                    usage_bytes INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_token TEXT,
                    lease_expires_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
        os.chmod(self.path, 0o600)

    @staticmethod
    def _status(row: sqlite3.Row) -> EgressActionStatus:
        return EgressActionStatus(
            action_id=row["action_id"],
            request_digest=row["request_digest"],
            state=row["state"],
            result=(json.loads(row["result_json"]) if row["result_json"] else None),
            error_code=row["error_code"],
            usage=EgressUsage(
                requests=row["usage_requests"], bytes=row["usage_bytes"]
            ),
            attempts=row["attempts"],
        )

    async def get(self, action_id: str) -> EgressActionStatus | None:
        async with self._lock:
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT * FROM egress_actions WHERE action_id = ?", (action_id,)
                ).fetchone()
                return None if row is None else self._status(row)

    async def claim(self, command: EgressActionCommand) -> EgressClaim:
        now = time.time()
        digest = command.request_digest
        command_json = canonical_json(command.model_dump(mode="json"))
        async with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT * FROM egress_actions WHERE action_id = ?",
                    (command.action_id,),
                ).fetchone()
                if row is not None:
                    if (
                        row["request_digest"] != digest
                        or row["command_json"] != command_json
                    ):
                        raise EgressStoreConflict(
                            "egress action id is bound to another request"
                        )
                    if row["state"] in {"succeeded", "failed"}:
                        connection.commit()
                        return EgressClaim(self._status(row), None)
                    if (
                        row["state"] == "processing"
                        and row["lease_expires_at"] is not None
                        and float(row["lease_expires_at"]) > now
                    ):
                        connection.commit()
                        return EgressClaim(self._status(row), None)
                    if int(row["attempts"]) >= self.max_attempts:
                        connection.execute(
                            """
                            UPDATE egress_actions
                            SET state = 'failed', error_code = 'retry_exhausted',
                                lease_token = NULL, lease_expires_at = NULL,
                                updated_at = ?
                            WHERE action_id = ?
                            """,
                            (now, command.action_id),
                        )
                        terminal = connection.execute(
                            "SELECT * FROM egress_actions WHERE action_id = ?",
                            (command.action_id,),
                        ).fetchone()
                        connection.commit()
                        return EgressClaim(self._status(terminal), None)

                lease_token = str(uuid.uuid4())
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO egress_actions (
                            action_id, request_digest, command_json, state,
                            attempts, lease_token, lease_expires_at,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, 'processing', 1, ?, ?, ?, ?)
                        """,
                        (
                            command.action_id,
                            digest,
                            command_json,
                            lease_token,
                            now + self.lease_seconds,
                            now,
                            now,
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE egress_actions
                        SET state = 'processing', attempts = attempts + 1,
                            lease_token = ?, lease_expires_at = ?, updated_at = ?
                        WHERE action_id = ?
                        """,
                        (
                            lease_token,
                            now + self.lease_seconds,
                            now,
                            command.action_id,
                        ),
                    )
                claimed = connection.execute(
                    "SELECT * FROM egress_actions WHERE action_id = ?",
                    (command.action_id,),
                ).fetchone()
                connection.commit()
                return EgressClaim(self._status(claimed), lease_token)
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    async def complete(
        self,
        *,
        action_id: str,
        lease_token: str,
        result: dict | None,
        error_code: str | None,
        usage: EgressUsage,
    ) -> EgressActionStatus:
        state = "succeeded" if error_code is None else "failed"
        result_json = canonical_json(result) if result is not None else None
        now = time.time()
        async with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE egress_actions
                    SET state = ?, result_json = ?, error_code = ?,
                        usage_requests = ?, usage_bytes = ?,
                        lease_token = NULL, lease_expires_at = NULL,
                        updated_at = ?
                    WHERE action_id = ? AND state = 'processing'
                      AND lease_token = ?
                    """,
                    (
                        state,
                        result_json,
                        error_code,
                        usage.requests,
                        usage.bytes,
                        now,
                        action_id,
                        lease_token,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM egress_actions WHERE action_id = ?", (action_id,)
                ).fetchone()
                if row is None or cursor.rowcount != 1:
                    raise EgressStoreConflict("egress action lease was lost")
                connection.commit()
                return self._status(row)
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    async def health(self) -> bool:
        try:
            async with self._lock:
                with self._connect() as connection:
                    return connection.execute("SELECT 1").fetchone()[0] == 1
        except sqlite3.Error:
            return False
