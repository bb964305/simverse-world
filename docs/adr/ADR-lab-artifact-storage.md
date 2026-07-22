# ADR: Lab Artifact storage and scan chain

- **Status: Accepted / executable service boundary present; production blocked**
- Date: 2026-07-22
- Scope: quarantine ingest, exact-version scanning/promotion, controlled release,
  and exact-version cleanup

## Decision

Production v2 artifacts use a private, versioned object-storage chain. Runtime
and Executor outputs do not become application artifacts by supplying inline
text or an arbitrary URI. Bytes first enter quarantine through Artifact Ingest,
are scanned by Artifact Scanner, and only a clean exact version is copied to the
released zone. Artifact Cleanup deletes named versions and returns durable delete
proofs.

The existing DB-backed body/URI fields remain a legacy, flag-off compatibility
path. They are not the production blob store, and a URI-string digest is not
accepted as proof of object bytes.

## Service boundary implemented

The independent package under `backend/app/lab/artifact_services/` provides:

- strict command/receipt schemas with canonical request and payload digests;
- audience-bound short-lived service JWT validation;
- one-object upload capabilities bound to tenant, run, session, artifact,
  producer action, epoch, operation ID, byte cap, MIME declaration, and optional
  expected digest;
- durable SQLite/WAL operation stores with idempotent same-ID/same-digest replay,
  conflict rejection, owner-fenced claims, and restart recovery;
- streaming Ingest with byte cap, actual SHA-256/size calculation, MIME sniffing,
  immutable quarantine write, signed upload receipt, terminal receipt lookup,
  and expired-lease recovery from its durable exact locator;
- asynchronous Scanner work with MIME allowlist, an isolated parser subprocess,
  archive/image/text limits, external malware engine timeout, retry state,
  exact-version download, and clean promotion;
- exact-version Cleanup with durable per-target absence proofs;
- filesystem and S3-compatible storage adapters without a new dependency.

The executable roles are:

```text
python -m app.lab.artifact_services.ingest.main
python -m app.lab.artifact_services.scanner.main
python -m app.lab.artifact_services.cleanup.main
```

Each role has a private environment namespace and refuses incomplete
configuration. Ingest is configured only for quarantine. Scanner receives
quarantine read plus released write. Cleanup receives exact delete access to
both. Production IAM, not application convention alone, must enforce those
permissions.

## Storage contract

Production storage is S3-compatible and must provide:

1. Private `quarantine` and `released` buckets with versioning enabled.
2. No public ACLs and no bucket-wide credentials in Runtime, Executor, Runner,
   API, or frontend processes.
3. Platform-generated immutable keys; callers cannot select arbitrary paths.
4. Every persisted locator contains backend, zone, bucket, key, version ID,
   ETag, actual byte size, actual SHA-256, and content type.
5. Reads, promotion, and deletion always address a specific version ID.
6. Released keys are never overwritten; promotion creates a new released
   version and verifies its bytes before returning `clean`.

The built-in SigV4 adapter intentionally supports only the needed PUT, exact GET,
exact DELETE, and bucket-versioning readiness operations. It does not list
buckets. Redirects and ambient proxy credentials are disabled. A storage
response without a version ID is rejected. Uploads use one bounded streaming PUT;
partial local streams are removed on failure, so this adapter does not create an
untracked multipart upload to abort.

The filesystem adapter exists for a single-host staging/reference deployment.
It uses immutable version files and enforces Scanner's quarantine zone as
read-only, but it is not multi-node production evidence.

## State transitions

The chain preserves three independent meanings:

```text
storage:      pending_upload -> quarantined -> released -> delete_pending -> deleted
scan:         pending -> scanning -> clean | flagged | failed
verification: unverified -> verified | rejected
```

The flow is:

1. Gateway creates a durable artifact row and bounded upload lease through
   Ingest for each Runtime artifact or Executor output declaration.
2. Runtime uploads one spooled stream with the one-object capability. For an
   Executor output, the stopped OCI sandbox is paused, declared scratch paths
   are copied into an Executor-private spool, and only validated regular files
   are uploaded with their corresponding leases. Ingest computes size, SHA-256,
   and MIME from actual bytes in either path.
3. A successful upload receipt identifies an immutable quarantine version.
   Declared size/digest mismatch fails the operation and cannot advance to scan.
   If bytes were already written when validation failed, the signed failure
   receipt retains the exact quarantine locator. Gateway ACKs that terminal
   receipt so Runtime may remove its spool copy; Executor instead retains its
   durable staged state until the same receipt is embedded in the terminal job
   result. The object remains isolated for cleanup.
   Ingest persists that locator before signing the terminal receipt. Gateway
   recovers both pre-submit and post-submit uncertainty with the original upload
   ID; an expired unconsumed lease becomes a signed failed receipt instead of
   remaining pending forever.
4. Gateway validates the Runtime receipt or the Executor manifest/receipt
   envelope against the original declaration, applies it idempotently, accounts
   actual bytes once, and submits a deterministic scan job for that exact
   quarantine version.
5. Scanner downloads and verifies the exact bytes, applies the configured
   policy, then either flags/fails or writes and verifies a released version.
6. Only the signed `clean` receipt with both exact references may produce
   `released/clean/verified` application state.
7. Download remains behind application ACL, task-release, scan, verification,
   and exact-version digest gates. No quarantine locator or permanent storage
   URL is exposed.
8. Retention creates a version-pinned delete command. DB tombstoning follows
   confirmed Cleanup receipts, never precedes them.

Every operation verifies its durable command JSON against its stored digest
before any network or object-store side effect. Claim-owner fencing prevents an
expired worker from overwriting the response of a newer claimant. Stored
receipts are also revalidated against their recorded digest before replay.

Timeouts are uncertain outcomes. Scan and delete callers continue the original
operation ID; a new attempt ID is permitted only after a terminal failure or an
authenticated explicit not-found result after the command deadline. Upload
similarly replays or queries the same lease/receipt while its outcome is
uncertain and may allocate a bounded new attempt only after the original lease
is terminal or explicitly absent after expiry.

## Scanner policy

Scanner startup requires a named policy version, engine version, MIME allowlist,
and an executable malware command containing exactly one `{path}` placeholder.
Readiness fails if that engine is absent. JSON, ZIP, PDF, PNG, JPEG, CSV, and
UTF-8 text enter a `python -I` parser worker with CPU/address-space/time bounds.
Archive depth, member count, expanded bytes, nested size, compression ratio,
image pixels/decoded bytes, text fields, CSV columns, file size, and scanner time
are bounded. Unsupported formats never fall through to a permissive parser.

`flagged` and exhausted `failed` states remain quarantined. Neither can be
silently translated to clean, verified, accepted, auto-released, or downloadable.

Receipt schemas recompute each canonical payload digest in addition to validating
the detached signature, request digest, service action, tenant/run/session/artifact,
producer action, operation ID, and epoch binding. A signed but internally
inconsistent receipt cannot advance application state.

## Authentication and receipts

- Ingest, Scanner, and Cleanup use different JWT audiences and current/next
  verification keyrings.
- Upload capabilities use a separate `lab-artifact-upload` audience and a short
  lease-bounded lifetime.
- Each service signs canonical receipts through a replaceable signer boundary.
- The production path uses Ed25519 (`EdDSA`) via OpenSSL and a workload-local,
  read-only private PEM. Gateway/API verification trust contains only read-only
  public PEM paths, with distinct current/next keys and distinct issuers for
  Ingest, Scanner, and Cleanup. Global admission and the release gate reject the
  checked-in HS256 staging implementation.
- Production must bind those mounts to an approved KMS/HSM or equivalent
  externally controlled trust root through D0; repository-generated keys or
  writable key files are not approval evidence.
- mTLS is additive to JWT binding in production.

## Release, retention, and cleanup boundary

Task and artifact detail responses expose manifests only. The single download
route checks tenant ACL, task release, `released/clean/verified`, the complete
upload/scan receipt chain, and the exact released object version before reading
bytes. The API receives a released-only reader identity and has no quarantine,
write, delete, or list capability.

Retention is represented by auditable `task`, `world_proposal`, `manual`, or
`legal` hold rows. Source-managed holds are reconciled when their source appears
or disappears; manual/legal holds have idempotent create and explicit release
paths. The legacy boolean remains only as an active-hold projection during
migration. Cleanup retains all locators and retry state until a signed receipt
proves every requested exact version is absent, then writes the tombstone and
clears locators. Holds fence both released-object retention deletion and cleanup
of the promoted quarantine version. Those two purposes have independent retry
budgets. An expired Artifact that provably never acquired an exact object
version is tombstoned locally only after every upload operation is terminal and
no stored receipt carries a quarantine locator; uncertain uploads remain
quarantined.

Pending rows use a short hours-scale TTL, quarantine/failed rows use a separate
days-scale TTL, and released rows use the product retention window. Promotion
residue cleanup does not shorten released retention.

## Deployment status and remaining gate

Compose supplies separate state/work volumes, a quarantine read-only mount for
Scanner, Runtime-to-Ingest and Executor-to-Ingest upload networks, and `/readyz`
healthchecks under the disabled `lab-production` profile. Executor's spool is
inside its existing private volume and has no object-store credentials. The
filesystem default is intentionally a reference mode. Production must switch
each storage role to independently credentialed S3 storage and prove
versioning/IAM/network policy. The production orchestrator must also
mount each service's Ed25519 private key and the API/Runner public trust set at
the absolute paths named by configuration. Compose provides explicit read-only
bind contracts for those external files/directories, defaulting to an invalid
`/dev/null` source so an unprovisioned production profile fails closed; it does
not create or approve signing keys.

This ADR does not claim that production is enabled. Image digests, object-store
provider/IAM, scanner supply chain, receipt trust root, mTLS, topology hashes,
and external attestation remain unresolved in
`.omx/approvals/lab-agent-services-d0.json`; D0 stays blocked.
Because the minimal storage adapter intentionally has no bucket-list capability,
provider-side inventory/reconciliation for the irreducible object-write versus
local-store crash window also remains an external operational control.

## Rejected

- Public buckets or permanent/presigned-at-write URLs, because they bypass the
  release gate.
- Server-side proxying of arbitrary Runtime URIs, because it creates an SSRF and
  mutable-content surface.
- Letting Runtime/Executor hold bucket credentials, because a compromised data
  plane could bypass quarantine or delete evidence.
- Treating declared digest/size or a URI-string digest as actual-byte evidence.
- In-place quarantine-to-released mutation, because it loses the exact version
  chain.
- Marking DB rows deleted before exact object-version absence is proven.
