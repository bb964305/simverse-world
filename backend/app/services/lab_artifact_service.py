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


class ArtifactQuarantined(ArtifactError):
    """The artifact is not yet scan-clean AND verified, so its body/URI must not
    leave the API (recovery plan Phase 5, gap #10). Routers map this to 409."""


def is_releasable(a: LabArtifact) -> bool:
    """Whether an artifact's body/URI may leave the API: scan-clean AND verified.
    A skipped/pending/flagged scan, or an unverified/rejected verification, keeps
    the content server-quarantined regardless of task-release state."""
    return a.scan_status == "clean" and a.verification_status == "verified"


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
    artifact.sha256 = compute_sha256(artifact)
    artifact.byte_size = len(_digest_content(artifact).encode("utf-8"))
    artifact.expires_at = datetime.now(UTC) + timedelta(days=settings.lab_artifact_retention_days)
    if scanned_clean:
        artifact.scan_status = "clean"
        artifact.verification_status = "verified"
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


async def cleanup_expired(db, *, now: datetime | None = None) -> dict:
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
    if not settings.lab_agent_v1_enabled:
        return {
            "deleted_count": 0, "held_count": 0, "quarantined_count": 0,
            "byte_count": 0, "tombstone_digest": None,
        }
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
