"""Durable provider registration and recovery for protocol-v2 sessions."""
from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.lab.protocol import RuntimeV2Handshake
from app.models.lab_lease import LabRunLease
from app.models.lab_run import LabRun
from app.models.lab_runtime import LabRuntimeSession


_CREATION_LEASE_SECONDS = 30
_CONCURRENT_WAIT_SECONDS = 5
_CONCURRENT_POLL_SECONDS = 0.02
_MAX_LOCATOR_BYTES = 4096


class RuntimeSessionError(RuntimeError):
    """The provider session cannot be opened without violating its binding."""


class RuntimeSessionQuarantined(RuntimeSessionError):
    """The provider locator is uncertain and the run must remain fenced."""


class RuntimeSessionInProgress(RuntimeSessionError):
    """Another live owner is still creating the same provider session."""


def client_run_id(run_id: str, epoch: int) -> str:
    """Return the stable provider idempotency key for one run/epoch binding."""
    if type(epoch) is not int or epoch < 0:
        raise RuntimeSessionError("runtime session epoch must be a non-negative integer")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"simverse:lab-runtime:{run_id}:{epoch}"))


def _provider_method(provider: Any, *names: str) -> Callable[..., Any] | None:
    for name in names:
        method = getattr(provider, name, None)
        if callable(method):
            return method
    return None


def _database_clock(db):
    if db.get_bind().dialect.name == "postgresql":
        return func.clock_timestamp()
    return func.current_timestamp()


def _live_lease_exists(
    *, run_id: str, owner_id: str, epoch: int, clock
):
    return exists(
        select(LabRunLease.run_id).where(
            LabRunLease.run_id == run_id,
            LabRunLease.owner_id == owner_id,
            LabRunLease.fencing_epoch == epoch,
            LabRunLease.expires_at > clock,
        )
    )


async def _assert_live_lease(
    db, *, run_id: str, owner_id: str, epoch: int
) -> None:
    if not isinstance(owner_id, str) or not owner_id.strip() or len(owner_id) > 80:
        raise RuntimeSessionError("runtime session lease owner is invalid")
    if type(epoch) is not int or epoch < 0:
        raise RuntimeSessionError("runtime session epoch must be a non-negative integer")
    bound = await db.scalar(
        select(LabRunLease.run_id).where(
            LabRunLease.run_id == run_id,
            LabRunLease.owner_id == owner_id,
            LabRunLease.fencing_epoch == epoch,
            LabRunLease.expires_at > _database_clock(db),
        )
    )
    if bound is None:
        raise RuntimeSessionError(
            "runtime session requires the caller's current live lease"
        )


async def _lock_live_lease(
    db, *, run_id: str, owner_id: str, epoch: int
) -> None:
    """Hold the matching lease row through the registration transaction."""
    locked = await db.scalar(
        select(LabRunLease.run_id)
        .where(
            LabRunLease.run_id == run_id,
            LabRunLease.owner_id == owner_id,
            LabRunLease.fencing_epoch == epoch,
        )
        .with_for_update()
    )
    if locked is None:
        raise RuntimeSessionError(
            "runtime session requires the caller's current live lease"
        )
    still_live = await db.scalar(
        select(LabRunLease.run_id).where(
            LabRunLease.run_id == run_id,
            LabRunLease.owner_id == owner_id,
            LabRunLease.fencing_epoch == epoch,
            LabRunLease.expires_at > _database_clock(db),
        )
    )
    if still_live is None:
        raise RuntimeSessionError(
            "runtime session requires the caller's current live lease"
        )


async def _lock_runtime_session(db, session_id: str) -> LabRuntimeSession:
    session = (
        await db.execute(
            select(LabRuntimeSession)
            .where(LabRuntimeSession.id == session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if session is None:
        raise RuntimeSessionError("runtime session vanished during transition")
    return session


async def _provider_handshake(
    provider: Any, *, expected_durability: str
) -> RuntimeV2Handshake:
    handshake = _provider_method(provider, "handshake", "runtime_handshake")
    create = _provider_method(provider, "create_session", "create")
    reattach = _provider_method(provider, "reattach_session", "reattach")
    if handshake is None or create is None or reattach is None:
        raise RuntimeSessionError(
            "provider must support handshake, idempotent create, and reattach"
        )
    raw = handshake()
    if inspect.isawaitable(raw):
        raw = await raw
    try:
        manifest = RuntimeV2Handshake.model_validate(raw)
    except ValidationError as exc:
        raise RuntimeSessionError("provider v2 handshake is invalid") from exc
    if manifest.durability_class != expected_durability:
        raise RuntimeSessionError("provider handshake durability binding changed")
    explicit_name = getattr(provider, "name", None)
    if explicit_name is not None and explicit_name != manifest.provider_name:
        raise RuntimeSessionError("provider handshake name does not match provider")
    return manifest


def _assert_json_locator(value: Any) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if type(value) in {int, float}:
        return
    if isinstance(value, list):
        for item in value:
            _assert_json_locator(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise RuntimeSessionError(
                    "provider locator object keys must be non-empty strings"
                )
            _assert_json_locator(item)
        return
    raise RuntimeSessionError("provider locator must contain only JSON values")


def _provider_binding(result: Any) -> tuple[dict, str | None, str]:
    if isinstance(result, dict):
        values = dict(result)
    else:
        values = {
            name: getattr(result, name)
            for name in ("locator", "session_id", "durability_class")
            if getattr(result, name, None) is not None
        }
    locator = values.get("locator")
    provider_session_id = values.get("session_id")
    durability_class = values.get("durability_class")
    if not isinstance(durability_class, str) or not durability_class:
        raise RuntimeSessionError("provider result omitted durability_class")
    if provider_session_id is not None and (
        not isinstance(provider_session_id, str)
        or not provider_session_id.strip()
        or len(provider_session_id) > 200
    ):
        raise RuntimeSessionError("provider session_id must be a non-empty string")
    if locator is None and provider_session_id is None:
        raise RuntimeSessionError("provider returned no durable session locator")
    if isinstance(locator, dict):
        if not locator:
            raise RuntimeSessionError("provider locator object must not be empty")
        _assert_json_locator(locator)
        locator_json = dict(locator)
    elif isinstance(locator, str):
        if not locator.strip():
            raise RuntimeSessionError("provider locator must be a non-empty string")
        locator_json = {"locator": locator}
    elif locator is None:
        assert provider_session_id is not None
        locator_json = {"locator": provider_session_id}
    else:
        raise RuntimeSessionError(
            "provider locator must be a non-empty string or object"
        )
    try:
        encoded = json.dumps(
            locator_json,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RuntimeSessionError("provider locator must be canonical JSON") from exc
    if len(encoded) > _MAX_LOCATOR_BYTES:
        raise RuntimeSessionError(
            f"provider locator exceeds {_MAX_LOCATOR_BYTES} bytes"
        )
    return locator_json, provider_session_id, durability_class


async def _quarantine_creating(
    db, session_id: str, *, owner: str, reason: str
) -> None:
    await db.execute(
        update(LabRuntimeSession)
        .where(
            LabRuntimeSession.id == session_id,
            LabRuntimeSession.status == "creating",
            LabRuntimeSession.creation_owner == owner,
        )
        .values(
            status="quarantined",
            creation_owner=None,
            creation_lease_expires_at=None,
            last_error=reason[:500],
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    db.expire_all()


async def _quarantine_ready(db, session_id: str, *, reason: str) -> None:
    await db.execute(
        update(LabRuntimeSession)
        .where(
            LabRuntimeSession.id == session_id,
            LabRuntimeSession.status == "ready",
        )
        .values(status="quarantined", last_error=reason[:500])
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    db.expire_all()


async def _release_creation_owner(db, session_id: str, *, owner: str) -> None:
    await db.execute(
        update(LabRuntimeSession)
        .where(
            LabRuntimeSession.id == session_id,
            LabRuntimeSession.status == "creating",
            LabRuntimeSession.creation_owner == owner,
        )
        .values(creation_owner=None, creation_lease_expires_at=None)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    db.expire_all()


async def _discard_unstarted_registration(
    db, session_id: str, *, owner: str
) -> None:
    await db.execute(
        delete(LabRuntimeSession).where(
            LabRuntimeSession.id == session_id,
            LabRuntimeSession.status == "creating",
            LabRuntimeSession.creation_owner == owner,
            LabRuntimeSession.provider_session_id.is_(None),
            LabRuntimeSession.locator_json.is_(None),
        )
    )
    await db.commit()
    db.expire_all()


async def _mark_ready(
    db,
    *,
    session_id: str,
    run_id: str,
    epoch: int,
    owner: str,
    lease_owner_id: str,
    result: Any,
    expected_durability: str,
) -> LabRuntimeSession:
    locator_json, provider_session_id, actual_durability = _provider_binding(result)
    if actual_durability is not None and actual_durability != expected_durability:
        await _quarantine_creating(
            db,
            session_id,
            owner=owner,
            reason=(
                "provider durability mismatch: "
                f"expected {expected_durability}, got {actual_durability}"
            ),
        )
        raise RuntimeSessionQuarantined("provider durability class changed")

    normalized_provider_session_id = (
        str(provider_session_id) if provider_session_id is not None else None
    )
    try:
        await _lock_live_lease(
            db, run_id=run_id, owner_id=lease_owner_id, epoch=epoch
        )
        current = await _lock_runtime_session(db, session_id)
        if (
            current.fencing_epoch != epoch
            or current.status != "creating"
            or current.creation_owner != owner
        ):
            raise RuntimeSessionError(
                f"runtime session left creating state during provider binding: "
                f"{current.status}"
            )

        # The session-row wait can outlive the lease TTL. Recheck only after
        # both rows are locked, then check once more after the UPDATE so a slow
        # database trigger cannot commit a stale ready transition.
        await _assert_live_lease(
            db, run_id=run_id, owner_id=lease_owner_id, epoch=epoch
        )
        claimed = await db.execute(
            update(LabRuntimeSession)
            .where(
                LabRuntimeSession.id == session_id,
                LabRuntimeSession.fencing_epoch == epoch,
                LabRuntimeSession.status == "creating",
                LabRuntimeSession.creation_owner == owner,
                _live_lease_exists(
                    run_id=run_id,
                    owner_id=lease_owner_id,
                    epoch=epoch,
                    clock=_database_clock(db),
                ),
            )
            .values(
                status="ready",
                provider_session_id=normalized_provider_session_id,
                locator_json=locator_json,
                creation_owner=None,
                creation_lease_expires_at=None,
                last_error=None,
            )
            .execution_options(synchronize_session=False)
        )
        if (claimed.rowcount or 0) != 1:
            raise RuntimeSessionError(
                "runtime session lost its live lease during provider binding"
            )
        await _assert_live_lease(
            db, run_id=run_id, owner_id=lease_owner_id, epoch=epoch
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise

    db.expire_all()
    ready = await db.get(LabRuntimeSession, session_id)
    if ready is None or ready.status != "ready":
        raise RuntimeSessionError("runtime session vanished during provider binding")
    if (
        ready.locator_json != locator_json
        or ready.provider_session_id != normalized_provider_session_id
    ):
        raise RuntimeSessionError(
            "provider returned a divergent locator for an already-ready session"
        )
    return ready


def _validate_existing_binding(
    session: LabRuntimeSession,
    *,
    stable_client_id: str,
    epoch: int,
    durability_class: str,
    provider_name: str,
) -> None:
    if session.client_run_id != stable_client_id or session.fencing_epoch != epoch:
        raise RuntimeSessionError(
            "runtime session is already bound to a different fencing epoch"
        )
    if session.durability_class != durability_class:
        raise RuntimeSessionError("runtime session durability binding changed")
    if session.provider_name != provider_name:
        raise RuntimeSessionError("runtime session provider binding changed")


async def _register_or_claim(
    db,
    *,
    run_id: str,
    stable_client_id: str,
    epoch: int,
    lease_owner_id: str,
    durability_class: str,
    provider_name: str,
    owner: str,
) -> tuple[LabRuntimeSession, str]:
    wait_deadline = time.monotonic() + _CONCURRENT_WAIT_SECONDS
    while True:
        await _lock_live_lease(
            db, run_id=run_id, owner_id=lease_owner_id, epoch=epoch
        )
        session = (
            await db.execute(
                select(LabRuntimeSession).where(LabRuntimeSession.run_id == run_id)
            )
        ).scalar_one_or_none()
        if session is None:
            session = LabRuntimeSession(
                run_id=run_id,
                client_run_id=stable_client_id,
                fencing_epoch=epoch,
                protocol_version=2,
                provider_name=provider_name,
                durability_class=durability_class,
                status="creating",
                creation_owner=owner,
                creation_lease_expires_at=(
                    datetime.now(UTC) + timedelta(seconds=_CREATION_LEASE_SECONDS)
                ),
            )
            db.add(session)
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                winner = (
                    await db.execute(
                        select(LabRuntimeSession.id).where(
                            LabRuntimeSession.run_id == run_id
                        )
                    )
                ).scalar_one_or_none()
                if winner is None:
                    raise RuntimeSessionError(
                        "runtime session registration failed without a concurrent winner"
                    ) from exc
                continue
            await db.refresh(session)
            return session, "create"

        try:
            _validate_existing_binding(
                session,
                stable_client_id=stable_client_id,
                epoch=epoch,
                durability_class=durability_class,
                provider_name=provider_name,
            )
        except RuntimeSessionError:
            await db.rollback()
            raise
        if session.status == "ready":
            await db.commit()
            return session, "verify"
        if session.status != "creating":
            await db.rollback()
            if session.status == "quarantined":
                raise RuntimeSessionQuarantined(
                    session.last_error or "runtime session is quarantined"
                )
            raise RuntimeSessionError(
                f"runtime session cannot attach from state {session.status}"
            )

        now = datetime.now(UTC)
        session_id = session.id
        claimed = await db.execute(
            update(LabRuntimeSession)
            .where(
                LabRuntimeSession.id == session_id,
                LabRuntimeSession.status == "creating",
                or_(
                    LabRuntimeSession.creation_owner.is_(None),
                    LabRuntimeSession.creation_lease_expires_at.is_(None),
                    LabRuntimeSession.creation_lease_expires_at <= now,
                ),
            )
            .values(
                creation_owner=owner,
                creation_lease_expires_at=(
                    now + timedelta(seconds=_CREATION_LEASE_SECONDS)
                ),
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        db.expire_all()
        if (claimed.rowcount or 0) == 1:
            current = await db.get(LabRuntimeSession, session_id)
            if current is None:
                raise RuntimeSessionError("runtime session vanished while claimed")
            return current, "reattach"
        if time.monotonic() >= wait_deadline:
            raise RuntimeSessionInProgress(
                "runtime session creation is still owned by a live attempt"
            )
        await asyncio.sleep(_CONCURRENT_POLL_SECONDS)


async def _verify_ready(
    db,
    *,
    session: LabRuntimeSession,
    run_id: str,
    epoch: int,
    lease_owner_id: str,
    result: Any,
    expected_durability: str,
) -> LabRuntimeSession:
    session_id = session.id
    try:
        locator_json, provider_session_id, actual_durability = _provider_binding(result)
    except RuntimeSessionError as exc:
        await _quarantine_ready(db, session_id, reason=str(exc))
        raise RuntimeSessionQuarantined(str(exc)) from exc
    normalized_session_id = (
        str(provider_session_id) if provider_session_id is not None else None
    )
    try:
        await _lock_live_lease(
            db, run_id=run_id, owner_id=lease_owner_id, epoch=epoch
        )
        current = await _lock_runtime_session(db, session_id)
        if current.status != "ready" or current.fencing_epoch != epoch:
            raise RuntimeSessionError(
                f"runtime session was fenced during provider verification: "
                f"{current.status}"
            )
        await _assert_live_lease(
            db, run_id=run_id, owner_id=lease_owner_id, epoch=epoch
        )
        if (
            actual_durability != expected_durability
            or current.locator_json != locator_json
            or current.provider_session_id != normalized_session_id
        ):
            await db.rollback()
            await _quarantine_ready(
                db, session_id, reason="provider ready-session binding diverged"
            )
            raise RuntimeSessionQuarantined(
                "provider ready-session binding diverged"
            )

        verified = await db.execute(
            update(LabRuntimeSession)
            .where(
                LabRuntimeSession.id == session_id,
                LabRuntimeSession.status == "ready",
                LabRuntimeSession.fencing_epoch == epoch,
                _live_lease_exists(
                    run_id=run_id,
                    owner_id=lease_owner_id,
                    epoch=epoch,
                    clock=_database_clock(db),
                ),
            )
            .values(updated_at=LabRuntimeSession.updated_at)
            .execution_options(synchronize_session=False)
        )
        if (verified.rowcount or 0) != 1:
            raise RuntimeSessionError(
                "runtime session lost its live lease during provider verification"
            )
        await _assert_live_lease(
            db, run_id=run_id, owner_id=lease_owner_id, epoch=epoch
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise

    db.expire_all()
    ready = await db.get(LabRuntimeSession, session_id)
    if ready is None or ready.status != "ready":
        raise RuntimeSessionError("runtime session vanished during provider verification")
    return ready


async def create_or_reattach(
    db,
    *,
    run_id: str,
    epoch: int,
    owner_id: str,
    provider: Any,
    durability_class: str,
) -> LabRuntimeSession:
    """Create once, then recover only by the same deterministic provider id.

    The ``creating`` registration commits before any provider call. If the
    provider created a session but the caller lost its response, the next call
    invokes reattach and never manufactures a second provider session.
    """
    if durability_class != "session_affine":
        raise RuntimeSessionError(
            "only the verified session_affine durability class is supported"
        )
    stable_client_id = client_run_id(run_id, epoch)
    run = await db.get(LabRun, run_id)
    if run is None:
        raise RuntimeSessionError("runtime session run does not exist")
    if run.protocol_version != 2:
        raise RuntimeSessionError("runtime sessions require protocol_version 2")
    if run.adapter != "simverse_ref":
        raise RuntimeSessionError(
            "protocol-v2 runtime sessions require the simverse_ref adapter"
        )
    await _assert_live_lease(db, run_id=run_id, owner_id=owner_id, epoch=epoch)
    await db.commit()
    manifest = await _provider_handshake(
        provider, expected_durability=durability_class
    )

    owner = str(uuid.uuid4())
    session, mode = await _register_or_claim(
        db,
        run_id=run_id,
        stable_client_id=stable_client_id,
        epoch=epoch,
        lease_owner_id=owner_id,
        durability_class=durability_class,
        provider_name=manifest.provider_name,
        owner=owner,
    )
    session_id = session.id

    create = _provider_method(provider, "create_session", "create")
    reattach = _provider_method(provider, "reattach_session", "reattach")
    method = create if mode == "create" else reattach
    assert method is not None  # proven by the handshake gate
    provider_called = False
    try:
        await _assert_live_lease(db, run_id=run_id, owner_id=owner_id, epoch=epoch)
        await db.commit()
        provider_called = True
        result = await method(client_run_id=stable_client_id, epoch=epoch)
        if result is None:
            if mode == "verify":
                await _quarantine_ready(
                    db, session_id, reason="session-affine provider host was lost"
                )
            else:
                await _quarantine_creating(
                    db,
                    session_id,
                    owner=owner,
                    reason="session-affine provider host was lost",
                )
            raise RuntimeSessionQuarantined("session-affine provider host was lost")
        if mode == "verify":
            return await _verify_ready(
                db,
                session=session,
                run_id=run_id,
                epoch=epoch,
                lease_owner_id=owner_id,
                result=result,
                expected_durability=durability_class,
            )
        try:
            return await _mark_ready(
                db,
                session_id=session_id,
                run_id=run_id,
                epoch=epoch,
                owner=owner,
                lease_owner_id=owner_id,
                result=result,
                expected_durability=durability_class,
            )
        except RuntimeSessionError as exc:
            await _quarantine_creating(
                db, session_id, owner=owner, reason=str(exc)
            )
            raise
    except BaseException:
        await db.rollback()
        if mode == "create" and not provider_called:
            await _discard_unstarted_registration(db, session_id, owner=owner)
        elif mode != "verify":
            await _release_creation_owner(db, session_id, owner=owner)
        raise
