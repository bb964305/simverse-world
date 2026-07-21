"""Durable, Runtime-local protocol-v2 state backed by SQLite.

This store owns model-side session/checkpoint state, replayable provider events,
pending intent bindings, artifacts, and service-command deduplication.  It does
not replace the Gateway's PostgreSQL turn/action/event truth.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

import aiosqlite

from app.lab.runtime_ref.service_auth import MAX_REQUEST_BYTES, canonical_json_bytes


STORE_VERSION = 1
SESSION_STATES = frozenset({
    "created", "running", "intent_pending", "resuming", "completed", "failed",
    "cancelled", "fenced", "quarantined",
})
INTENT_STATES = frozenset({"pending", "result_recorded", "applied"})
COMMAND_STATES = frozenset({"accepted", "completed"})
RESULT_OUTCOMES = frozenset({"succeeded", "denied", "failed"})
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_UNSET = object()


class RuntimeStoreError(RuntimeError):
    pass


class RuntimeStoreConflict(RuntimeStoreError):
    pass


class RuntimeStoreNotFound(RuntimeStoreError):
    pass


class CrossBindingReplay(RuntimeStoreConflict):
    """A jti or command id was reused outside its original exact binding."""


@dataclass(frozen=True)
class StoredSession:
    session_id: str
    run_id: str
    client_run_id: str
    epoch: int
    scopes: tuple[str, ...]
    budget_usd: float
    egress_allowlist: tuple[str, ...]
    state: str
    checkpoint: Any | None
    next_event_cursor: int


@dataclass(frozen=True)
class StoredEvent:
    session_id: str
    cursor: int
    event_id: str
    event_kind: str
    turn_id: str | None
    intent_id: str | None
    outcome: str | None
    payload: Any
    dedupe_key: str | None


@dataclass(frozen=True)
class StoredIntent:
    session_id: str
    turn_id: str
    intent_id: str
    tool: str
    args: Any
    state: str
    result_digest: str | None
    result_outcome: str | None
    result_payload: Any | None


@dataclass(frozen=True)
class StoredArtifact:
    session_id: str
    artifact_id: str
    kind: str
    title: str
    uri: str | None
    text_md: str | None
    meta: Any


@dataclass(frozen=True)
class CommandBinding:
    audience: str
    command_id: str
    jti: str
    request_digest: str
    run_id: str
    session_id: str
    epoch: int
    action: str


@dataclass(frozen=True)
class StoredCommandReceipt:
    receipt_id: str
    binding: CommandBinding
    state: str
    response: Any | None


@dataclass(frozen=True)
class CommandClaim:
    receipt: StoredCommandReceipt
    is_retry: bool


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS runtime_sessions (
        session_id TEXT PRIMARY KEY,
        run_id TEXT NOT NULL,
        client_run_id TEXT NOT NULL,
        epoch INTEGER NOT NULL CHECK (epoch >= 0),
        scopes_json TEXT NOT NULL,
        budget_usd REAL NOT NULL CHECK (budget_usd >= 0),
        egress_allowlist_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN (
            'created', 'running', 'intent_pending', 'resuming', 'completed',
            'failed', 'cancelled', 'fenced', 'quarantined'
        )),
        checkpoint_json TEXT,
        next_event_cursor INTEGER NOT NULL DEFAULT 1 CHECK (next_event_cursor >= 1),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (client_run_id, epoch),
        UNIQUE (run_id, epoch)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_events (
        session_id TEXT NOT NULL REFERENCES runtime_sessions(session_id) ON DELETE CASCADE,
        cursor INTEGER NOT NULL CHECK (cursor >= 1),
        event_id TEXT NOT NULL UNIQUE,
        event_kind TEXT NOT NULL,
        turn_id TEXT,
        intent_id TEXT,
        outcome TEXT,
        payload_json TEXT NOT NULL,
        event_digest TEXT NOT NULL,
        dedupe_key TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (session_id, cursor),
        UNIQUE (session_id, dedupe_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_intents (
        session_id TEXT NOT NULL REFERENCES runtime_sessions(session_id) ON DELETE CASCADE,
        turn_id TEXT NOT NULL,
        intent_id TEXT NOT NULL,
        tool TEXT NOT NULL,
        args_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('pending', 'result_recorded', 'applied')),
        result_digest TEXT,
        result_outcome TEXT CHECK (
            result_outcome IS NULL OR result_outcome IN ('succeeded', 'denied', 'failed')
        ),
        result_payload_json TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (session_id, intent_id),
        UNIQUE (session_id, turn_id)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS uq_runtime_active_intent
    ON runtime_intents(session_id) WHERE state IN ('pending', 'result_recorded')
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_artifacts (
        session_id TEXT NOT NULL REFERENCES runtime_sessions(session_id) ON DELETE CASCADE,
        artifact_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        uri TEXT,
        text_md TEXT,
        meta_json TEXT NOT NULL,
        artifact_digest TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (session_id, artifact_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runtime_command_receipts (
        jti TEXT PRIMARY KEY,
        audience TEXT NOT NULL,
        command_id TEXT NOT NULL,
        request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
        run_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        epoch INTEGER NOT NULL CHECK (epoch >= 0),
        action TEXT NOT NULL,
        receipt_id TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK (state IN ('accepted', 'completed')),
        response_json TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (audience, command_id),
        CHECK (
            (state = 'accepted' AND response_json IS NULL)
            OR (state = 'completed' AND response_json IS NOT NULL)
        )
    )
    """,
)


def _canonical_text(value: Any) -> str:
    return canonical_json_bytes(value, max_bytes=MAX_REQUEST_BYTES).decode("utf-8")


def _load(value: str | None) -> Any | None:
    return None if value is None else json.loads(value)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _require_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is required")


class RuntimeStore:
    """Small transaction-oriented store intended for one session-affine Runtime."""

    def __init__(self, path: str | Path) -> None:
        raw_path = str(path)
        if not raw_path or raw_path == ":memory:" or "mode=memory" in raw_path:
            raise ValueError("protocol-v2 runtime_store_path must be a durable file")
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
                continue

    def _prepare_main_file(self) -> None:
        if os.name != "posix":
            return
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self._harden_files()

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._prepare_main_file()
            db = await aiosqlite.connect(self.path, isolation_level=None)
            try:
                await db.execute("PRAGMA foreign_keys = ON")
                await db.execute("PRAGMA journal_mode = WAL")
                await db.execute("PRAGMA busy_timeout = 5000")
                await db.execute("BEGIN IMMEDIATE")
                version = (await (await db.execute("PRAGMA user_version")).fetchone())[0]
                if version not in {0, STORE_VERSION}:
                    raise RuntimeStoreError(
                        f"unsupported runtime store version {version}; expected {STORE_VERSION}"
                    )
                for statement in _SCHEMA_STATEMENTS:
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
        self._harden_files()
        db = await aiosqlite.connect(self.path, isolation_level=None)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA busy_timeout = 5000")
        self._harden_files()
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
    def _session(row: aiosqlite.Row) -> StoredSession:
        return StoredSession(
            session_id=row["session_id"], run_id=row["run_id"],
            client_run_id=row["client_run_id"], epoch=row["epoch"],
            scopes=tuple(_load(row["scopes_json"]) or []), state=row["state"],
            budget_usd=float(row["budget_usd"]),
            egress_allowlist=tuple(_load(row["egress_allowlist_json"]) or []),
            checkpoint=_load(row["checkpoint_json"]),
            next_event_cursor=row["next_event_cursor"],
        )

    async def create_or_get_session(
        self,
        *,
        run_id: str,
        client_run_id: str,
        epoch: int,
        scopes: Iterable[str],
        budget_usd: float = 0.5,
        egress_allowlist: Iterable[str] = (),
        session_id: str | None = None,
    ) -> StoredSession:
        _require_text("run_id", run_id)
        _require_text("client_run_id", client_run_id)
        if type(epoch) is not int or epoch < 0:
            raise ValueError("epoch must be non-negative")
        normalized_scopes = tuple(sorted(set(scopes)))
        if any(not isinstance(scope, str) or not scope for scope in normalized_scopes):
            raise ValueError("scopes must be non-empty strings")
        if (
            isinstance(budget_usd, bool)
            or not isinstance(budget_usd, (int, float))
            or not math.isfinite(budget_usd)
        ):
            raise ValueError("budget_usd must be finite")
        if budget_usd < 0:
            raise ValueError("budget_usd must be non-negative")
        normalized_egress = tuple(sorted(set(egress_allowlist)))
        if any(not isinstance(host, str) or not host for host in normalized_egress):
            raise ValueError("egress_allowlist must contain non-empty strings")
        scopes_json = _canonical_text(list(normalized_scopes))
        egress_json = _canonical_text(list(normalized_egress))
        async with self._transaction() as db:
            rows = await (
                await db.execute(
                    "SELECT * FROM runtime_sessions "
                    "WHERE (client_run_id = ? AND epoch = ?) OR (run_id = ? AND epoch = ?)",
                    (client_run_id, epoch, run_id, epoch),
                )
            ).fetchall()
            by_id = {row["session_id"]: row for row in rows}
            if len(by_id) > 1:
                raise RuntimeStoreConflict("session binding resolves to multiple rows")
            if by_id:
                existing = self._session(next(iter(by_id.values())))
                if (
                    existing.run_id != run_id
                    or existing.client_run_id != client_run_id
                    or existing.epoch != epoch
                    or existing.scopes != normalized_scopes
                    or existing.budget_usd != float(budget_usd)
                    or existing.egress_allowlist != normalized_egress
                    or (session_id is not None and existing.session_id != session_id)
                ):
                    raise RuntimeStoreConflict("session binding mismatch")
                return existing

            new_session_id = session_id or f"ref-{uuid.uuid4().hex[:16]}"
            _require_text("session_id", new_session_id)
            await db.execute(
                "INSERT INTO runtime_sessions "
                "(session_id, run_id, client_run_id, epoch, scopes_json, budget_usd, "
                "egress_allowlist_json, state) VALUES (?, ?, ?, ?, ?, ?, ?, 'created')",
                (
                    new_session_id, run_id, client_run_id, epoch, scopes_json,
                    float(budget_usd), egress_json,
                ),
            )
            row = await (
                await db.execute(
                    "SELECT * FROM runtime_sessions WHERE session_id = ?", (new_session_id,)
                )
            ).fetchone()
            return self._session(row)

    async def get_session(self, session_id: str) -> StoredSession | None:
        db = await self._connect()
        try:
            row = await (
                await db.execute(
                    "SELECT * FROM runtime_sessions WHERE session_id = ?", (session_id,)
                )
            ).fetchone()
            return None if row is None else self._session(row)
        finally:
            await db.close()

    async def transition_session(
        self,
        session_id: str,
        *,
        expected_states: str | Iterable[str],
        new_state: str,
        checkpoint: Any = _UNSET,
    ) -> StoredSession:
        expected = {expected_states} if isinstance(expected_states, str) else set(expected_states)
        if not expected or not expected <= SESSION_STATES or new_state not in SESSION_STATES:
            raise ValueError("unknown session state")
        checkpoint_json = None if checkpoint is _UNSET else _canonical_text(checkpoint)
        async with self._transaction() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM runtime_sessions WHERE session_id = ?", (session_id,)
                )
            ).fetchone()
            if row is None:
                raise RuntimeStoreNotFound("session not found")
            if row["state"] not in expected:
                raise RuntimeStoreConflict(
                    f"session state {row['state']!r} is not one of {sorted(expected)!r}"
                )
            if checkpoint is _UNSET:
                await db.execute(
                    "UPDATE runtime_sessions SET state = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE session_id = ?",
                    (new_state, session_id),
                )
            else:
                await db.execute(
                    "UPDATE runtime_sessions SET state = ?, checkpoint_json = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                    (new_state, checkpoint_json, session_id),
                )
            updated = await (
                await db.execute(
                    "SELECT * FROM runtime_sessions WHERE session_id = ?", (session_id,)
                )
            ).fetchone()
            return self._session(updated)

    @staticmethod
    def _event(row: aiosqlite.Row) -> StoredEvent:
        return StoredEvent(
            session_id=row["session_id"], cursor=row["cursor"], event_id=row["event_id"],
            event_kind=row["event_kind"], turn_id=row["turn_id"],
            intent_id=row["intent_id"], outcome=row["outcome"],
            payload=_load(row["payload_json"]), dedupe_key=row["dedupe_key"],
        )

    async def append_event(
        self,
        session_id: str,
        *,
        event_kind: str,
        payload: Any,
        turn_id: str | None = None,
        intent_id: str | None = None,
        outcome: str | None = None,
        event_id: str | None = None,
        dedupe_key: str | None = None,
    ) -> StoredEvent:
        _require_text("event_kind", event_kind)
        payload_json = _canonical_text(payload)
        content = {
            "event_kind": event_kind, "turn_id": turn_id, "intent_id": intent_id,
            "outcome": outcome, "payload": payload,
        }
        event_digest = _digest(content)
        async with self._transaction() as db:
            if dedupe_key is not None:
                existing = await (
                    await db.execute(
                        "SELECT * FROM runtime_events WHERE session_id = ? AND dedupe_key = ?",
                        (session_id, dedupe_key),
                    )
                ).fetchone()
                if existing is not None:
                    if existing["event_digest"] != event_digest:
                        raise RuntimeStoreConflict("event dedupe key payload mismatch")
                    return self._event(existing)
            session = await (
                await db.execute(
                    "SELECT next_event_cursor FROM runtime_sessions WHERE session_id = ?",
                    (session_id,),
                )
            ).fetchone()
            if session is None:
                raise RuntimeStoreNotFound("session not found")
            cursor = session["next_event_cursor"]
            stored_event_id = event_id or str(uuid.uuid4())
            await db.execute(
                "INSERT INTO runtime_events "
                "(session_id, cursor, event_id, event_kind, turn_id, intent_id, outcome, "
                "payload_json, event_digest, dedupe_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id, cursor, stored_event_id, event_kind, turn_id, intent_id,
                    outcome, payload_json, event_digest, dedupe_key,
                ),
            )
            await db.execute(
                "UPDATE runtime_sessions SET next_event_cursor = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE session_id = ?",
                (cursor + 1, session_id),
            )
            row = await (
                await db.execute(
                    "SELECT * FROM runtime_events WHERE session_id = ? AND cursor = ?",
                    (session_id, cursor),
                )
            ).fetchone()
            return self._event(row)

    async def list_events(
        self, session_id: str, *, after: int = 0, limit: int = 1000
    ) -> list[StoredEvent]:
        if (
            type(after) is not int
            or type(limit) is not int
            or after < 0
            or not 1 <= limit <= 1000
        ):
            raise ValueError("invalid event replay window")
        db = await self._connect()
        try:
            rows = await (
                await db.execute(
                    "SELECT * FROM runtime_events WHERE session_id = ? AND cursor > ? "
                    "ORDER BY cursor LIMIT ?",
                    (session_id, after, limit),
                )
            ).fetchall()
            return [self._event(row) for row in rows]
        finally:
            await db.close()

    @staticmethod
    def _intent(row: aiosqlite.Row) -> StoredIntent:
        return StoredIntent(
            session_id=row["session_id"], turn_id=row["turn_id"],
            intent_id=row["intent_id"], tool=row["tool"], args=_load(row["args_json"]),
            state=row["state"], result_digest=row["result_digest"],
            result_outcome=row["result_outcome"],
            result_payload=_load(row["result_payload_json"]),
        )

    async def record_intent(
        self, session_id: str, *, turn_id: str, intent_id: str, tool: str, args: Any
    ) -> StoredIntent:
        for name, value in (("turn_id", turn_id), ("intent_id", intent_id), ("tool", tool)):
            _require_text(name, value)
        args_json = _canonical_text(args)
        async with self._transaction() as db:
            existing = await (
                await db.execute(
                    "SELECT * FROM runtime_intents WHERE session_id = ? AND intent_id = ?",
                    (session_id, intent_id),
                )
            ).fetchone()
            if existing is not None:
                value = self._intent(existing)
                if value.turn_id != turn_id or value.tool != tool or value.args != args:
                    raise RuntimeStoreConflict("intent binding mismatch")
                return value
            try:
                await db.execute(
                    "INSERT INTO runtime_intents "
                    "(session_id, turn_id, intent_id, tool, args_json, state) "
                    "VALUES (?, ?, ?, ?, ?, 'pending')",
                    (session_id, turn_id, intent_id, tool, args_json),
                )
            except aiosqlite.IntegrityError as exc:
                raise RuntimeStoreConflict("session already has a pending intent or turn") from exc
            row = await (
                await db.execute(
                    "SELECT * FROM runtime_intents WHERE session_id = ? AND intent_id = ?",
                    (session_id, intent_id),
                )
            ).fetchone()
            return self._intent(row)

    async def get_intent(self, session_id: str, intent_id: str) -> StoredIntent | None:
        db = await self._connect()
        try:
            row = await (
                await db.execute(
                    "SELECT * FROM runtime_intents WHERE session_id = ? AND intent_id = ?",
                    (session_id, intent_id),
                )
            ).fetchone()
            return None if row is None else self._intent(row)
        finally:
            await db.close()

    async def count_active_intents(self, session_id: str) -> int:
        """Return intents that still block final/artifact publication."""

        db = await self._connect()
        try:
            row = await (
                await db.execute(
                    "SELECT count(*) FROM runtime_intents "
                    "WHERE session_id = ? AND state IN ('pending', 'result_recorded')",
                    (session_id,),
                )
            ).fetchone()
            return int(row[0])
        finally:
            await db.close()

    async def resolve_intent(
        self,
        session_id: str,
        *,
        intent_id: str,
        result_digest: str,
        outcome: str,
        payload: Any,
    ) -> StoredIntent:
        if not _DIGEST_RE.fullmatch(result_digest):
            raise ValueError("result_digest must be lowercase sha256")
        if outcome not in RESULT_OUTCOMES:
            raise ValueError("unknown result outcome")
        if _digest(payload) != result_digest:
            raise RuntimeStoreConflict("result digest does not match payload")
        payload_json = _canonical_text(payload)
        async with self._transaction() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM runtime_intents WHERE session_id = ? AND intent_id = ?",
                    (session_id, intent_id),
                )
            ).fetchone()
            if row is None:
                raise RuntimeStoreNotFound("intent not found")
            existing = self._intent(row)
            if existing.state != "pending":
                if (
                    existing.result_digest == result_digest
                    and existing.result_outcome == outcome
                    and existing.result_payload == payload
                ):
                    return existing
                raise RuntimeStoreConflict("intent already has a different result")
            await db.execute(
                "UPDATE runtime_intents SET state = 'result_recorded', result_digest = ?, "
                "result_outcome = ?, result_payload_json = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE session_id = ? AND intent_id = ?",
                (result_digest, outcome, payload_json, session_id, intent_id),
            )
            updated = await (
                await db.execute(
                    "SELECT * FROM runtime_intents WHERE session_id = ? AND intent_id = ?",
                    (session_id, intent_id),
                )
            ).fetchone()
            return self._intent(updated)

    async def mark_intent_applied(self, session_id: str, intent_id: str) -> StoredIntent:
        async with self._transaction() as db:
            cursor = await db.execute(
                "UPDATE runtime_intents SET state = 'applied', updated_at = CURRENT_TIMESTAMP "
                "WHERE session_id = ? AND intent_id = ? AND state = 'result_recorded'",
                (session_id, intent_id),
            )
            if cursor.rowcount != 1:
                row = await (
                    await db.execute(
                        "SELECT * FROM runtime_intents WHERE session_id = ? AND intent_id = ?",
                        (session_id, intent_id),
                    )
                ).fetchone()
                if row is None:
                    raise RuntimeStoreNotFound("intent not found")
                if row["state"] != "applied":
                    raise RuntimeStoreConflict("intent result is not recorded")
            row = await (
                await db.execute(
                    "SELECT * FROM runtime_intents WHERE session_id = ? AND intent_id = ?",
                    (session_id, intent_id),
                )
            ).fetchone()
            return self._intent(row)

    @staticmethod
    def _artifact(row: aiosqlite.Row) -> StoredArtifact:
        return StoredArtifact(
            session_id=row["session_id"], artifact_id=row["artifact_id"],
            kind=row["kind"], title=row["title"], uri=row["uri"],
            text_md=row["text_md"], meta=_load(row["meta_json"]),
        )

    async def put_artifact(
        self,
        session_id: str,
        *,
        artifact_id: str,
        kind: str,
        title: str,
        uri: str | None = None,
        text_md: str | None = None,
        meta: Any | None = None,
    ) -> StoredArtifact:
        for name, value in (("artifact_id", artifact_id), ("kind", kind), ("title", title)):
            _require_text(name, value)
        meta_value = {} if meta is None else meta
        meta_json = _canonical_text(meta_value)
        content = {
            "kind": kind, "title": title, "uri": uri, "text_md": text_md,
            "meta": meta_value,
        }
        artifact_digest = _digest(content)
        async with self._transaction() as db:
            existing = await (
                await db.execute(
                    "SELECT * FROM runtime_artifacts WHERE session_id = ? AND artifact_id = ?",
                    (session_id, artifact_id),
                )
            ).fetchone()
            if existing is not None:
                if existing["artifact_digest"] != artifact_digest:
                    raise RuntimeStoreConflict("artifact id payload mismatch")
                return self._artifact(existing)
            await db.execute(
                "INSERT INTO runtime_artifacts "
                "(session_id, artifact_id, kind, title, uri, text_md, meta_json, artifact_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id, artifact_id, kind, title, uri, text_md, meta_json,
                    artifact_digest,
                ),
            )
            row = await (
                await db.execute(
                    "SELECT * FROM runtime_artifacts WHERE session_id = ? AND artifact_id = ?",
                    (session_id, artifact_id),
                )
            ).fetchone()
            return self._artifact(row)

    async def list_artifacts(self, session_id: str) -> list[StoredArtifact]:
        db = await self._connect()
        try:
            rows = await (
                await db.execute(
                    "SELECT * FROM runtime_artifacts WHERE session_id = ? "
                    "ORDER BY created_at, artifact_id",
                    (session_id,),
                )
            ).fetchall()
            return [self._artifact(row) for row in rows]
        finally:
            await db.close()

    @staticmethod
    def _command(row: aiosqlite.Row) -> StoredCommandReceipt:
        binding = CommandBinding(
            audience=row["audience"], command_id=row["command_id"], jti=row["jti"],
            request_digest=row["request_digest"], run_id=row["run_id"],
            session_id=row["session_id"], epoch=row["epoch"], action=row["action"],
        )
        return StoredCommandReceipt(
            receipt_id=row["receipt_id"], binding=binding, state=row["state"],
            response=_load(row["response_json"]),
        )

    @staticmethod
    def _validate_binding(binding: CommandBinding) -> None:
        for name in (
            "audience", "command_id", "jti", "run_id", "session_id", "action",
        ):
            _require_text(name, getattr(binding, name))
        if type(binding.epoch) is not int or binding.epoch < 0:
            raise ValueError("epoch must be non-negative")
        if not _DIGEST_RE.fullmatch(binding.request_digest):
            raise ValueError("request_digest must be lowercase sha256")

    async def claim_command(self, binding: CommandBinding) -> CommandClaim:
        """Persist the first binding or return its exact durable retry receipt."""

        self._validate_binding(binding)
        async with self._transaction() as db:
            by_jti = await (
                await db.execute(
                    "SELECT * FROM runtime_command_receipts WHERE jti = ?", (binding.jti,)
                )
            ).fetchone()
            by_command = await (
                await db.execute(
                    "SELECT * FROM runtime_command_receipts "
                    "WHERE audience = ? AND command_id = ?",
                    (binding.audience, binding.command_id),
                )
            ).fetchone()
            existing_rows = [row for row in (by_jti, by_command) if row is not None]
            if existing_rows:
                if any(self._command(row).binding != binding for row in existing_rows):
                    raise CrossBindingReplay("command token binding mismatch")
                receipt = self._command(existing_rows[0])
                return CommandClaim(receipt=receipt, is_retry=True)

            receipt_id = str(uuid.uuid4())
            await db.execute(
                "INSERT INTO runtime_command_receipts "
                "(jti, audience, command_id, request_digest, run_id, session_id, epoch, "
                "action, receipt_id, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted')",
                (
                    binding.jti, binding.audience, binding.command_id,
                    binding.request_digest, binding.run_id, binding.session_id,
                    binding.epoch, binding.action, receipt_id,
                ),
            )
            row = await (
                await db.execute(
                    "SELECT * FROM runtime_command_receipts WHERE jti = ?", (binding.jti,)
                )
            ).fetchone()
            return CommandClaim(receipt=self._command(row), is_retry=False)

    async def complete_command(
        self, binding: CommandBinding, *, response: Any
    ) -> StoredCommandReceipt:
        self._validate_binding(binding)
        response_json = _canonical_text(response)
        async with self._transaction() as db:
            row = await (
                await db.execute(
                    "SELECT * FROM runtime_command_receipts WHERE jti = ?", (binding.jti,)
                )
            ).fetchone()
            if row is None:
                raise RuntimeStoreNotFound("command receipt not found")
            existing = self._command(row)
            if existing.binding != binding:
                raise CrossBindingReplay("command token binding mismatch")
            if isinstance(response, dict):
                if response.get("receipt_id", existing.receipt_id) != existing.receipt_id:
                    raise RuntimeStoreConflict("response receipt_id mismatch")
                if (
                    response.get("request_digest", binding.request_digest)
                    != binding.request_digest
                ):
                    raise RuntimeStoreConflict("response request_digest mismatch")
            if existing.state == "completed":
                if existing.response != response:
                    raise RuntimeStoreConflict("completed command response mismatch")
                return existing
            await db.execute(
                "UPDATE runtime_command_receipts SET state = 'completed', response_json = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE jti = ?",
                (response_json, binding.jti),
            )
            updated = await (
                await db.execute(
                    "SELECT * FROM runtime_command_receipts WHERE jti = ?", (binding.jti,)
                )
            ).fetchone()
            return self._command(updated)

    async def get_command(self, jti: str) -> StoredCommandReceipt | None:
        db = await self._connect()
        try:
            row = await (
                await db.execute(
                    "SELECT * FROM runtime_command_receipts WHERE jti = ?", (jti,)
                )
            ).fetchone()
            return None if row is None else self._command(row)
        finally:
            await db.close()
