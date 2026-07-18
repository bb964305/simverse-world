"""V12 artifact integrity + retention — DB-slice on the existing LabArtifact
(PRD §Artifacts and World Proposals, §Instruction and Memory Layers retention
section; scope ruling: no object store, that's P3 — see
.superpowers/sdd/task-9-brief.md).

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
* ``cleanup_expired`` sweeps ``expires_at < now`` rows that are NOT held: the
  row survives (audit trail), but its content is tombstoned — text_md/uri
  cleared, meta_json gets ``{"tombstone": <sha256>, "cleaned_at": ...}``. This
  is the DB-internal equivalent of "delete the object, keep the record". A
  held row is counted, not touched. A single row's tombstone failure is
  quarantined (``meta_json.cleanup_failed = true``) rather than aborting the
  whole batch — cleanup runs nightly and must make forward progress even if
  one row is pathological.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import settings
from app.lab import acl
from app.models.lab_artifact import LabArtifact
from app.models.lab_event import OutboxEvent
from app.models.lab_task import LabTask
from app.models.world_change_proposal import WorldChangeProposal


class ArtifactError(Exception):
    """Base for artifact-service failures."""


class DigestMismatch(ArtifactError):
    """Stored content no longer matches its recorded digest — retrieval blocked."""


def compute_sha256(a: LabArtifact) -> str:
    """utf-8 sha256 of ``text_md`` (preferred) or ``uri``; both empty → the
    digest of the empty string (no special-cased sentinel — just hash
    whatever content there is, including none)."""
    content = a.text_md if a.text_md is not None else (a.uri or "")
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


async def finalize_artifact(
    db, *, artifact: LabArtifact, tenant_id: str, producer_action_id: str | None = None,
) -> LabArtifact:
    """Stamp integrity/retention fields on a just-created artifact. Does not
    commit — the caller (orchestrator._succeed) commits the whole batch."""
    artifact.tenant_id = tenant_id
    artifact.producer_action_id = producer_action_id
    content = artifact.text_md if artifact.text_md is not None else (artifact.uri or "")
    artifact.sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    artifact.byte_size = len(content.encode("utf-8"))
    artifact.expires_at = datetime.now(UTC) + timedelta(days=settings.lab_artifact_retention_days)
    return artifact


async def verify_and_get(db, *, artifact_id: str, user_id: str, is_admin: bool) -> LabArtifact:
    """ACL + digest-verified read. Raises ``acl.AclDenied`` (routers → 404) for
    a missing/foreign artifact, ``DigestMismatch`` (routers → 409) if the
    stored content no longer matches its recorded digest. A NULL ``sha256``
    (never finalized — legacy row) skips the digest check entirely."""
    art = await db.get(LabArtifact, artifact_id)
    if art is None:
        raise acl.AclDenied("artifact not found")
    task = await db.get(LabTask, art.task_id)
    if task is None or not acl.can_read_task(task, user_id=user_id, is_admin=is_admin):
        raise acl.AclDenied("artifact not found")
    if art.sha256 is not None and compute_sha256(art) != art.sha256:
        raise DigestMismatch(f"artifact {artifact_id} digest mismatch")
    return art


async def apply_retention_holds(db) -> int:
    """Pin (retention_hold=True) every artifact still referenced by something
    else in the system. v1 judgment call (brief §范围裁定): a task's artifacts
    are referenced once the task is ``completed`` (the issuer accepted the
    result); a run's artifacts are referenced once a ``lab_run``-origin world
    proposal cites that run (``origin_ref == run_id``). Returns the count of
    rows newly held — an already-held row is not re-counted."""
    newly_held = 0

    completed_task_ids = (
        await db.execute(select(LabTask.id).where(LabTask.status == "completed"))
    ).scalars().all()
    if completed_task_ids:
        rows = (await db.execute(
            select(LabArtifact).where(
                LabArtifact.task_id.in_(completed_task_ids),
                LabArtifact.retention_hold.is_(False),
            )
        )).scalars().all()
        for a in rows:
            a.retention_hold = True
            newly_held += 1

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
                LabArtifact.retention_hold.is_(False),
            )
        )).scalars().all()
        for a in rows:
            a.retention_hold = True
            newly_held += 1

    await db.commit()
    return newly_held


def _tombstone_row(a: LabArtifact, *, now: datetime) -> str:
    """Clear one artifact's content, leaving the row + a tombstone marker in
    its meta_json. Returns the digest recorded in the tombstone. Raising here
    is the injection point ``cleanup_expired`` quarantines around — it must
    never take down the rest of the sweep."""
    digest = a.sha256 or compute_sha256(a)
    a.text_md = None
    a.uri = None
    meta = dict(a.meta_json or {})
    meta["tombstone"] = digest
    meta["cleaned_at"] = now.isoformat()
    a.meta_json = meta
    return digest


async def cleanup_expired(db, *, now: datetime | None = None) -> dict:
    """Sweep artifacts past their retention window. A held row is counted, not
    touched — a hold always wins over expiry. The rest get tombstoned (content
    cleared, row + audit trail kept) and the batch writes one outbox
    ``cleanup.completed`` event with its stats. A per-row failure is
    quarantined (``meta_json.cleanup_failed = true``, counted, skipped) rather
    than aborting the batch."""
    now = now if now is not None else datetime.now(UTC)
    deleted_count = 0
    held_count = 0
    quarantined_count = 0
    byte_count = 0
    tombstone_hashes: list[str] = []

    rows = (await db.execute(
        select(LabArtifact).where(
            LabArtifact.expires_at.isnot(None), LabArtifact.expires_at < now,
        )
    )).scalars().all()

    for a in rows:
        if a.retention_hold:
            held_count += 1
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

    # Sorted so the roll-up digest is deterministic regardless of row-fetch
    # order (SQLite has no ORDER BY here).
    tombstone_digest = hashlib.sha256("".join(sorted(tombstone_hashes)).encode("utf-8")).hexdigest()

    stats = {
        "deleted_count": deleted_count,
        "held_count": held_count,
        "quarantined_count": quarantined_count,
        "byte_count": byte_count,
        "tombstone_digest": tombstone_digest,
    }
    db.add(OutboxEvent(
        event_id=str(uuid.uuid4()),
        tenant_id="system",  # cross-tenant sweep — no single owning tenant
        run_id=None,
        topic="cleanup.completed",
        payload_json={"scope": "lab_artifacts", **stats},
    ))
    await db.commit()
    return stats
