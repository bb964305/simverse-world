# ADR: Lab artifact storage + controlled-download contract

- **Status: Proposed / contract documented, SDK adoption pending approval**
- Date: 2026-07-20
- Context: recovery plan Phase 5 — "Select and document a production
  object-storage/controlled-download contract before real runtime enablement; do
  not add an SDK dependency without approval."

## Decision (what is DECIDED and IMPLEMENTED now)

Artifacts are **DB-backed** in v1 (`lab_artifacts.text_md` / `uri` + a sha256
digest, byte size, provenance, scan/verification status, retention/expiry). There
is **no object store yet** and none is added here — adding an S3/GCS SDK is
explicitly deferred pending approval.

Content leaves the API **only through the controlled boundary already shipped**:

- `serialize_artifact` exposes body/URI only when the task is released AND the
  artifact is `scan_status="clean"` AND `verification_status="verified"`
  (`is_releasable`); everything else is server-quarantined.
- `GET /lab/artifacts/{id}/download` is the authenticated, digest-checking
  download seam: ACL-owned (else 404), digest-intact (else 409),
  clean+verified (else 409 quarantined), task released (else 423 Locked). Text
  streams as an attachment with `X-Content-SHA256` + `nosniff`; a remote URI is
  **never** server-proxied (SSRF surface) — its verified URI is returned as
  metadata for the owner to fetch.
- The frontend renders released Markdown with inert links + no remote image
  fetch.

## Production object-storage contract (to adopt when approved)

When a real Adapter (Phase 7) can emit large/binary artifacts, migrate blob bytes
out of the DB behind the SAME boundary, preserving every invariant above:

1. **Backend:** an S3-compatible object store (MinIO in staging, S3/GCS in prod).
   The row keeps the digest + a storage locator (`bucket/key`), not the bytes.
2. **Write path:** the executor writes bytes to a quarantine bucket; the row is
   `scan_status="pending"`. A scanner promotes clean → verified and moves/marks
   the object releasable. No public bucket ACLs — objects are private.
3. **Read path:** the download endpoint keeps owning auth + digest + release gate,
   then streams from the store OR issues a **short-lived, single-object,
   digest-pinned presigned GET** (never a bucket-wide or long-lived URL).
4. **SDK:** `boto3`/`aioboto3` (S3) — a NEW dependency, so it needs explicit
   approval before it is added. Until then the DB-backed path above is the store.
5. **Migration:** additive columns (`storage_bucket`, `storage_key`) +
   a backfill; the digest/ACL/release contract is unchanged, so the boundary and
   its tests carry over.

## Consequences

- No unverified body/URI can leave the API today (DB-backed) or after migration
  (private buckets + the same gate).
- No SDK is added without approval; the contract is fixed so adoption is a
  bounded, reviewable change, not a redesign.

## Rejected

- Public/presigned-at-write URLs — bypass the release gate.
- Server-side proxying of arbitrary artifact URIs — SSRF surface.
- Storing large blobs in Postgres indefinitely — bloats the DB; the migration
  path above supersedes it once approved.
