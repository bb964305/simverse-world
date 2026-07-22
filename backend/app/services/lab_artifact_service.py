"""Artifact integrity, release gating, retention holds, and object cleanup.

Four operations:

* ``finalize_artifact`` stamps tenant/digest/size/expiry onto a freshly
  produced artifact (called from the orchestrator's ``_succeed``, before
  commit). Legacy (flag-off) artifacts never go through this — they keep
  ``sha256``/``tenant_id``/``expires_at`` all NULL, which is also why
  ``verify_and_get`` skips the digest check and ``cleanup_expired``'s query
  naturally skips them (NULL ``expires_at`` never matches ``< now``).
* ``verify_and_get`` is the read path: ACL (task owner or admin, else
  ``acl.AclDenied`` — routers turn that into 404, never 403) then a digest
  recheck (mismatch → ``DigestMismatch``, blocking retrieval of tampered
  content).
* ``apply_retention_holds`` pins evidence that's still referenced: every
  artifact of a completed task, and every artifact whose run produced a
  ``lab_run``-origin world-change proposal. Both are v1's judgment call
  (brief §范围裁定) — no ``world_revisions`` cross-check yet, that's future
  work once proposals apply through the full revision chain.
* ``cleanup_expired`` stages exact-version object deletion for production rows
  and retains the legacy DB tombstone path for historical rows.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import settings
from app.lab import acl
from app.models.lab_artifact import (
    LabArtifact,
    LabArtifactHold,
    LabArtifactOperation,
)
from app.models.lab_event import OutboxEvent
from app.models.lab_task import LabTask
from app.models.world_change_proposal import WorldChangeProposal


class ArtifactError(Exception):
    """Base for artifact-service failures."""


class DigestMismatch(ArtifactError):
    """Stored content no longer matches its recorded digest — retrieval blocked."""


class ArtifactQuarantined(ArtifactError):
    """The artifact is not yet scan-clean AND verified, so its body/URI must not
    leave the API (recovery plan Phase 5, gap #10). Routers map this to 409."""


async def get_manifest_for_user(
    db, *, artifact_id: str, user_id: str, is_admin: bool
) -> LabArtifact:
    """ACL-only metadata read; quarantined bytes remain inaccessible."""
    art = await db.get(LabArtifact, artifact_id)
    if art is None:
        raise acl.AclDenied("artifact not found")
    task = await db.get(LabTask, art.task_id)
    if task is None or not acl.can_read_task(
        task, user_id=user_id, is_admin=is_admin
    ):
        raise acl.AclDenied("artifact not found")
    return art


def is_releasable(a: LabArtifact) -> bool:
    """Whether an artifact's body/URI may leave the API: scan-clean AND verified.
    A skipped/pending/flagged scan, or an unverified/rejected verification, keeps
    the content server-quarantined regardless of task-release state."""
    integrity_ready = (
        a.scan_status == "clean" and a.verification_status == "verified"
    )
    if a.storage_status == "legacy":
        return integrity_ready
    return a.storage_status == "released" and integrity_ready


def _digest_content(a: LabArtifact) -> str:
    """The string an artifact's integrity digest/size are computed over,
    picked by ``kind`` rather than "whichever field is non-None": a
    ``kind="text"`` artifact always hashes ``text_md`` (empty counts as
    content); every other kind (link/file/image/dataset) hashes its ``uri``
    first, falling back to ``text_md`` only when ``uri`` is unset. Without the
    kind branch, a link artifact with ``text_md=""`` (a legitimate shape —
    e.g. the http adapter's collected artifacts) would hash an empty string
    and the digest would never reflect the actual ``uri`` content (P2-B
    review finding). Both empty → the digest of the empty string."""
    if a.kind == "text":
        return a.text_md or ""
    return a.uri or a.text_md or ""


def compute_sha256(a: LabArtifact) -> str:
    """utf-8 sha256 of ``_digest_content(a)``."""
    return hashlib.sha256(_digest_content(a).encode("utf-8")).hexdigest()


async def finalize_artifact(
    db, *, artifact: LabArtifact, tenant_id: str, producer_action_id: str | None = None,
    scanned_clean: bool = True,
) -> LabArtifact:
    """Stamp integrity/retention fields on a just-created artifact. Does not
    commit — the caller (orchestrator._succeed) commits the whole batch.

    ``scanned_clean`` is the TRUST BOUNDARY (recovery plan Phase 5, gap #10). The
    one real untrusted producer — a real runtime Adapter — passes ``False`` so its
    artifacts stay quarantined (scan_status skipped / unverified) until a real
    scanner clears them, and no unverified body/URI can leave the API. It defaults
    to ``True`` because the Mock adapter produces synthetic, safe content (nothing
    to scan) and legacy/test callers finalize trusted content; the orchestrator
    passes ``scanned_clean=(adapter == "mock")`` explicitly."""
    artifact.tenant_id = tenant_id
    artifact.producer_action_id = producer_action_id
    if (
        settings.lab_artifact_pending_ttl_hours <= 0
        or settings.lab_artifact_quarantine_ttl_days <= 0
        or settings.lab_artifact_retention_days <= 0
    ):
        raise ArtifactError("artifact lifecycle TTLs must be positive")
    production_record = bool(artifact.provider_artifact_id) or artifact.storage_status not in (
        None,
        "legacy",
    )
    if production_record:
        # Production rows are finalized only from an Ingest receipt. Runtime
        # declarations must never become the authoritative byte digest.
        artifact.storage_status = artifact.storage_status or "pending_upload"
        artifact.scan_status = "pending"
        artifact.verification_status = "unverified"
        artifact.expires_at = datetime.now(UTC) + timedelta(
            hours=settings.lab_artifact_pending_ttl_hours
        )
    else:
        artifact.storage_status = "legacy"
        artifact.expires_at = datetime.now(UTC) + timedelta(
            days=settings.lab_artifact_retention_days
        )
        artifact.sha256 = compute_sha256(artifact)
        artifact.byte_size = len(_digest_content(artifact).encode("utf-8"))
    if scanned_clean and not production_record:
        artifact.scan_status = "clean"
        artifact.verification_status = "verified"
    return artifact


async def verify_and_get(db, *, artifact_id: str, user_id: str, is_admin: bool) -> LabArtifact:
    """ACL + digest-verified read. Raises ``acl.AclDenied`` (routers → 404) for
    a missing/foreign artifact, ``DigestMismatch`` (routers → 409) if the
    stored content no longer matches its recorded digest. A NULL ``sha256``
    (never finalized — legacy row) skips the digest check entirely."""
    art = await get_manifest_for_user(
        db, artifact_id=artifact_id, user_id=user_id, is_admin=is_admin
    )
    if (
        art.storage_status == "legacy"
        and art.sha256 is not None
        and compute_sha256(art) != art.sha256
    ):
        raise DigestMismatch(f"artifact {artifact_id} digest mismatch")
    if art.storage_status != "legacy" and (
        not art.sha256
        or art.byte_size < 0
        or art.storage_backend not in {"filesystem", "s3"}
        or not art.content_type
        or not art.released_bucket
        or not art.released_key
        or not art.released_version_id
        or not art.released_etag
        or not art.upload_receipt_digest
        or not art.scan_receipt_digest
    ):
        raise ArtifactQuarantined(
            f"artifact {artifact_id} has an incomplete release receipt chain"
        )
    if art.storage_status != "legacy":
        from app.lab.artifact_pipeline import (
            ArtifactPipelineError,
            verify_released_artifact_chain,
        )

        try:
            await verify_released_artifact_chain(db, art)
        except ArtifactPipelineError as exc:
            raise ArtifactQuarantined(
                f"artifact {artifact_id} release receipt chain is invalid"
            ) from exc
    # Content-release gate: never hand back an unscanned/unverified body or remote
    # URI, even to the owner, even after task completion (gap #10). A legacy row
    # (never finalized: NULL sha256) predates this pipeline and is exempt.
    if art.sha256 is not None and not is_releasable(art):
        raise ArtifactQuarantined(f"artifact {artifact_id} not scan-clean+verified")
    return art


async def apply_retention_holds(db) -> int:
    """Pin (retention_hold=True) every artifact still referenced by something
    else in the system. v1 judgment call (brief §范围裁定): a task's artifacts
    are referenced once the task is ``completed`` (the issuer accepted the
    result); a run's artifacts are referenced once a ``lab_run``-origin world
    proposal cites that run (``origin_ref == run_id``). Returns the count of
    rows newly held — an already-held row is not re-counted."""
    newly_held = 0
    desired: set[tuple[str, str, str]] = set()

    async def ensure_managed_hold(
        artifact: LabArtifact, *, source_type: str, source_id: str, reason: str
    ) -> None:
        nonlocal newly_held
        desired.add((artifact.id, source_type, source_id))
        active = await db.scalar(
            select(LabArtifactHold.id)
            .where(
                LabArtifactHold.artifact_id == artifact.id,
                LabArtifactHold.source_type == source_type,
                LabArtifactHold.source_id == source_id,
                LabArtifactHold.released_at.is_(None),
            )
            .limit(1)
        )
        if active is None:
            db.add(
                LabArtifactHold(
                    id=str(uuid.uuid4()),
                    artifact_id=artifact.id,
                    reason=reason,
                    source_type=source_type,
                    source_id=source_id,
                )
            )
            newly_held += 1
        artifact.retention_hold = True

    completed_task_ids = (
        await db.execute(select(LabTask.id).where(LabTask.status == "completed"))
    ).scalars().all()
    if completed_task_ids:
        rows = (await db.execute(
            select(LabArtifact).where(
                LabArtifact.task_id.in_(completed_task_ids),
            )
        )).scalars().all()
        for a in rows:
            await ensure_managed_hold(
                a,
                source_type="task",
                source_id=a.task_id,
                reason="accepted_task_evidence",
            )

    referenced_run_ids = (await db.execute(
        select(WorldChangeProposal.origin_ref).where(
            WorldChangeProposal.origin == "lab_run",
            WorldChangeProposal.origin_ref.isnot(None),
        )
    )).scalars().all()
    if referenced_run_ids:
        # Autoflush (default on) makes the first pass's pending updates visible
        # here, so an artifact matching both criteria is not double-counted.
        rows = (await db.execute(
            select(LabArtifact).where(
                LabArtifact.run_id.in_(referenced_run_ids),
            )
        )).scalars().all()
        for a in rows:
            await ensure_managed_hold(
                a,
                source_type="world_proposal",
                source_id=a.run_id,
                reason="world_proposal_evidence",
            )

    released_artifact_ids: set[str] = set()
    active_managed_holds = (
        await db.execute(
            select(LabArtifactHold).where(
                LabArtifactHold.source_type.in_(("task", "world_proposal")),
                LabArtifactHold.released_at.is_(None),
            )
        )
    ).scalars().all()
    released_at = datetime.now(UTC)
    for hold in active_managed_holds:
        binding = (hold.artifact_id, hold.source_type, hold.source_id)
        if binding not in desired:
            hold.released_at = released_at
            released_artifact_ids.add(hold.artifact_id)

    await db.flush()
    for artifact_id in released_artifact_ids:
        artifact = await db.get(LabArtifact, artifact_id)
        if artifact is None:
            continue
        active = await db.scalar(
            select(LabArtifactHold.id)
            .where(
                LabArtifactHold.artifact_id == artifact_id,
                LabArtifactHold.released_at.is_(None),
            )
            .limit(1)
        )
        artifact.retention_hold = active is not None

    await db.commit()
    return newly_held


async def place_artifact_hold(
    db,
    *,
    artifact_id: str,
    reason: str,
    source_type: str,
    source_id: str,
) -> LabArtifactHold:
    """Create or return an active operator-managed retention hold."""
    if source_type not in {"manual", "legal"}:
        raise ArtifactError("operator-managed hold type must be manual or legal")
    for name, value, max_length in (
        ("reason", reason, 200),
        ("source_id", source_id, 200),
    ):
        if (
            not value
            or value != value.strip()
            or any(ord(char) < 32 for char in value)
            or len(value) > max_length
        ):
            raise ArtifactError(f"artifact hold {name} is invalid")
    artifact = await db.scalar(
        select(LabArtifact)
        .where(LabArtifact.id == artifact_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if artifact is None:
        raise ArtifactError("artifact not found")
    if artifact.storage_status in {"delete_pending", "deleted"}:
        raise ArtifactError(
            "artifact hold cannot be added after exact-version deletion begins"
        )
    existing = await db.scalar(
        select(LabArtifactHold)
        .where(
            LabArtifactHold.artifact_id == artifact_id,
            LabArtifactHold.source_type == source_type,
            LabArtifactHold.source_id == source_id,
            LabArtifactHold.released_at.is_(None),
        )
        .limit(1)
    )
    if existing is not None:
        if existing.reason != reason:
            raise ArtifactError("active artifact hold binding has a different reason")
        artifact.retention_hold = True
        await db.commit()
        return existing
    hold = LabArtifactHold(
        id=str(uuid.uuid4()),
        artifact_id=artifact_id,
        reason=reason,
        source_type=source_type,
        source_id=source_id,
    )
    db.add(hold)
    artifact.retention_hold = True
    await db.commit()
    return hold


async def release_artifact_hold(
    db, *, hold_id: str, released_at: datetime | None = None
) -> LabArtifactHold:
    """Release one auditable hold and refresh the legacy active-hold projection."""
    hold = await db.scalar(
        select(LabArtifactHold)
        .where(LabArtifactHold.id == hold_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if hold is None:
        raise ArtifactError("artifact hold not found")
    if hold.source_type in {"task", "world_proposal"}:
        raise ArtifactError("source-managed artifact hold cannot be released directly")
    if released_at is not None and (
        released_at.tzinfo is None or released_at.utcoffset() is None
    ):
        raise ArtifactError("artifact hold release time must be timezone-aware")
    if hold.released_at is None:
        hold.released_at = released_at or datetime.now(UTC)
    artifact = await db.scalar(
        select(LabArtifact)
        .where(LabArtifact.id == hold.artifact_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if artifact is None:
        raise ArtifactError("artifact hold references a missing artifact")
    remaining = await db.scalar(
        select(LabArtifactHold.id)
        .where(
            LabArtifactHold.artifact_id == artifact.id,
            LabArtifactHold.released_at.is_(None),
            LabArtifactHold.id != hold.id,
        )
        .limit(1)
    )
    artifact.retention_hold = remaining is not None
    await db.commit()
    return hold


async def _has_active_or_legacy_hold(db, artifact: LabArtifact) -> bool:
    holds = (
        await db.execute(
            select(LabArtifactHold.released_at).where(
                LabArtifactHold.artifact_id == artifact.id
            )
        )
    ).scalars().all()
    if not holds:
        # Preserve pre-migration boolean holds until they are explicitly adopted.
        return bool(artifact.retention_hold)
    active = any(released_at is None for released_at in holds)
    artifact.retention_hold = active
    return active


def _tombstone_row(a: LabArtifact, *, now: datetime) -> str:
    """Clear one artifact's content, leaving the row + a tombstone marker in
    its meta_json. Returns the digest recorded in the tombstone. Raising here
    is the injection point ``cleanup_expired`` quarantines around — it must
    never take down the rest of the sweep.

    Order matters: the tombstone marker is written BEFORE the content is
    cleared. A raise between the two steps then leaves the row with its
    content still intact and a (slightly premature) tombstone marker —
    recoverable. The reverse order (clear first, mark second) would leave a
    row with its content already gone and no tombstone recorded if
    interrupted mid-function — silently lost evidence with no trace
    (P2-B review finding)."""
    digest = a.sha256 or compute_sha256(a)
    meta = dict(a.meta_json or {})
    meta["tombstone"] = digest
    meta["cleaned_at"] = now.isoformat()
    a.meta_json = meta
    a.text_md = None
    a.uri = None
    return digest


async def cleanup_expired(
    db,
    *,
    now: datetime | None = None,
    pipeline_client=None,
) -> dict:
    """Sweep artifacts past their retention window. A held row is counted, not
    touched — a hold always wins over expiry. The rest get tombstoned (content
    cleared, row + audit trail kept) and the batch writes one outbox
    ``cleanup.completed`` event with its stats. A per-row failure is
    quarantined (``meta_json.cleanup_failed = true``, counted, skipped) rather
    than aborting the batch.

    Gated by ``settings.lab_agent_v1_enabled`` — unlike ``apply_retention_holds``
    (purely protective), this operation is destructive (clears content).
    Product decision (P2-B review): while the flag is off — e.g. during a
    rollback window — nothing gets destroyed even if the nightly sweep (or
    any other caller) still invokes this. Flag off is a hard no-op: no query,
    no mutation, no outbox event, all-zero stats returned."""
    if not (
        settings.lab_agent_v1_enabled
        or settings.lab_artifact_pipeline_enabled
    ):
        return {
            "deleted_count": 0, "held_count": 0, "quarantined_count": 0,
            "scheduled_count": 0, "byte_count": 0, "tombstone_digest": None,
        }
    now = now if now is not None else datetime.now(UTC)
    deleted_count = 0
    held_count = 0
    quarantined_count = 0
    scheduled_count = 0
    byte_count = 0
    tombstone_hashes: list[str] = []

    rows = (await db.execute(
        select(LabArtifact).where(
            LabArtifact.expires_at.isnot(None), LabArtifact.expires_at < now,
        ).order_by(LabArtifact.storage_status == "legacy", LabArtifact.id)
    )).scalars().all()

    for a in rows:
        if await _has_active_or_legacy_hold(db, a):
            held_count += 1
            continue
        if a.storage_status != "legacy":
            if a.storage_status in {"delete_pending", "deleted"}:
                continue
            if pipeline_client is None:
                quarantined_count += 1
                continue
            artifact_id = a.id
            run_id = a.run_id
            tenant_id = a.tenant_id
            try:
                has_exact_object = bool(
                    a.quarantine_version_id or a.released_version_id
                )
                if not has_exact_object:
                    if a.storage_status != "pending_upload":
                        raise ArtifactError(
                            "materialized artifact lost its exact object locator"
                        )
                    locked = await db.scalar(
                        select(LabArtifact)
                        .where(LabArtifact.id == a.id)
                        .with_for_update()
                        .execution_options(populate_existing=True)
                    )
                    if locked is None:
                        continue
                    a = locked
                    if await _has_active_or_legacy_hold(db, a):
                        held_count += 1
                        continue
                    if a.quarantine_version_id or a.released_version_id:
                        await pipeline_client.stage_delete(db, artifact=a)
                        scheduled_count += 1
                        continue
                    upload_operations = (
                        await db.execute(
                            select(LabArtifactOperation)
                            .where(
                                LabArtifactOperation.artifact_id == a.id,
                                LabArtifactOperation.operation_type == "upload",
                            )
                        )
                    ).scalars().all()
                    uncertain_upload = any(
                        operation.state in {"pending", "processing", "succeeded"}
                        or (
                            isinstance(operation.receipt_json, dict)
                            and operation.receipt_json.get("quarantine_ref")
                            is not None
                        )
                        for operation in upload_operations
                    )
                    if uncertain_upload:
                        raise ArtifactError(
                            "unmaterialized artifact still has an uncertain upload"
                        )
                    digest = _tombstone_row(a, now=now)
                    meta = dict(a.meta_json or {})
                    meta["cleanup_reason"] = "unmaterialized_upload_expired"
                    a.meta_json = meta
                    a.storage_status = "deleted"
                    a.deleted_at = now
                    a.scan_status = "failed"
                    a.verification_status = "rejected"
                    a.scan_error_code = (
                        a.scan_error_code or "unmaterialized_upload_expired"
                    )
                    await db.commit()
                    byte_count += a.byte_size or 0
                    tombstone_hashes.append(digest)
                    deleted_count += 1
                    continue
                await pipeline_client.stage_delete(db, artifact=a)
                scheduled_count += 1
            except Exception:
                await db.rollback()
                quarantined_count += 1
                from app.lab import telemetry
                telemetry.emit_alert(
                    telemetry.LabAlert.CLEANUP_QUARANTINE,
                    run_id=run_id,
                    tenant_id=tenant_id,
                    artifact_id=artifact_id,
                    reason="object_delete_staging_failed",
                )
            continue
        try:
            digest = _tombstone_row(a, now=now)
            byte_count += a.byte_size or 0
            tombstone_hashes.append(digest)
            deleted_count += 1
        except Exception:
            meta = dict(a.meta_json or {})
            meta["cleanup_failed"] = True
            a.meta_json = meta
            quarantined_count += 1
            from app.lab import telemetry
            telemetry.emit_alert(
                telemetry.LabAlert.CLEANUP_QUARANTINE,
                run_id=a.run_id, tenant_id=a.tenant_id, artifact_id=a.id, reason="tombstone_failed",
            )

    # Sorted so the roll-up digest is deterministic regardless of row-fetch
    # order (SQLite has no ORDER BY here).
    tombstone_digest = hashlib.sha256("".join(sorted(tombstone_hashes)).encode("utf-8")).hexdigest()

    stats = {
        "deleted_count": deleted_count,
        "held_count": held_count,
        "quarantined_count": quarantined_count,
        "scheduled_count": scheduled_count,
        "byte_count": byte_count,
        "tombstone_digest": tombstone_digest,
    }
    db.add(OutboxEvent(
        event_id=str(uuid.uuid4()),
        tenant_id="system",  # cross-tenant sweep — no single owning tenant
        run_id=None,
        topic="artifact.cleanup.completed",
        payload_json={"scope": "lab_artifacts", **stats},
    ))
    await db.commit()
    return stats
