# ADR: Lab production trust topology

- **Status: Accepted / executable reference topology present; production blocked**
- Date: 2026-07-22
- Scope: real Runtime/Executor deployment, restricted HTTP egress, and the
  Artifact ingest, scan, and cleanup trust planes

## Decision

The production Lab is a set of independently deployable workloads. Runtime,
Executor, restricted Egress, Artifact Ingest, Artifact Scanner, and Artifact
Cleanup must not be folded into the API, Runner, or each other. Each workload
has its own credentials, durable state, health endpoint, and network scope.

`deploy/backend/docker-compose.yml` now contains an opt-in reference graph under
the `lab-production` profile. It is a topology scaffold for staging and local
integration, not production authority. The normal Compose graph still excludes
all six workloads. The existing Runner remains separately opt-in under the
`lab` profile, so bringing up the reference graph requires both profiles and all
explicit feature gates.

## Workloads and identities

| Workload | Process | Required capability | Explicitly denied |
|---|---|---|---|
| API / control plane | `api` | app DB role, Redis, released-object read boundary | model key, OCI endpoint, quarantine read/write, object delete |
| Lab Runner / gateway | `lab-runner` | Runner DB role, Redis, scoped calls to each internal plane | model key, OCI endpoint, S3 credentials, direct object bytes |
| Runtime | `python -m app.lab.runtime_ref.main` | model-provider egress, shard state/spool, scoped Ingest upload path | Simverse DB, Redis, OCI host, released storage |
| Executor | `python -m app.lab.executor_service.main` | durable job/spool state, one dedicated rootless OCI endpoint, scoped one-object Ingest upload | Simverse DB, Redis, model provider, world writes, general egress, storage credentials |
| Restricted Egress | `python -m app.lab.egress_service.main` | bounded public HTTP(S), durable action state, configured SearXNG JSON provider | Simverse DB, Redis, model/OCI credentials, Artifact storage, private/metadata destinations |
| Artifact Ingest | `python -m app.lab.artifact_services.ingest.main` | create immutable quarantine versions | released storage, object delete, Simverse DB, Redis, model/OCI access |
| Artifact Scanner | `python -m app.lab.artifact_services.scanner.main` | exact quarantine reads and released writes | quarantine writes/deletes, Simverse DB, Redis, model/OCI access |
| Artifact Cleanup | `python -m app.lab.artifact_services.cleanup.main` | exact-version delete in quarantine and released zones | bucket listing, object creation, Simverse DB, Redis, model/OCI access |
| World Governor | proposal apply/revert path | overlay, revision, and outbox tables | model/OCI credentials and artifact storage credentials |

The Broker remains part of the Runner process, but protocol-v2 network effects
run only through the authenticated Egress workload. Runtime merely emits an
intent. Runner persists the exact action command, Egress resolves and pins a
public IP, and every redirect re-enters the same port, allowlist, and SSRF
checks. The Egress service receives no downstream application credentials.

## Network scopes

The reference Compose graph models separate point-to-point control networks:

- `lab-runtime-control`: Runner to Runtime only.
- `lab-executor-control`: Runner to Executor only.
- `lab-egress-control`: Runner to restricted Egress only.
- `lab-ingest-control`: Runner to Ingest only.
- `lab-scanner-control`: Runner to Scanner only.
- `lab-cleanup-control`: Runner to Cleanup only.
- `lab-runtime-upload`: Runtime to Ingest only.
- `lab-executor-upload`: Executor to Ingest only.
- `lab-runtime-model-egress`: Runtime outbound path.
- `lab-egress-public`: restricted Egress outbound path.
- Per-service storage egress networks for Ingest, Scanner, and Cleanup.
- `lab-api-storage-egress`: API to the released-only exact-version reader.

The control/upload networks are Compose `internal` networks. The external
egress networks are intentionally separate so workloads do not gain lateral
service discovery merely because they use the same provider. A production
platform must replace these names with enforced NetworkPolicy/firewall rules
that restrict destinations, including blocking cloud metadata and arbitrary
internet access. Compose network membership alone is not sufficient evidence.

Executor-to-Ingest is a capability-bound edge, not a storage edge. Gateway first
creates one durable `LabArtifact` and one bounded upload lease per declared
scratch path. Executor pauses the stopped sandbox, copies scratch into its
private spool, rejects links/path escapes/non-regular or oversized files,
computes the actual digest and size, then uploads with that exact one-object
lease. Executor never receives bucket credentials.

For supported protocol-v2 tools, Runner commits the canonical Executor command,
endpoint, job ID, epoch, and digest in the same transaction as the Broker's
`approved -> executing` claim. Recovery queries that exact locator and may
resubmit only the same command/job after a proven pre-submit `404`; an uncertain
accepted job is never replaced with a new ID. Unsupported tools converge to a
deterministic Broker failure after restart and are neither advertised by Runtime
nor sent to Mock.

## State and storage mounts

- Runtime state and spool use distinct persistent volumes.
- Executor durable job state and private output spool use an Executor-only
  volume.
- Restricted Egress durable action state uses an Egress-only volume.
- Ingest operation state and transient spool are separate from object storage.
- Scanner operation state and work space are Scanner-only.
- Cleanup operation state is Cleanup-only.
- In filesystem reference mode, Scanner mounts quarantine read-only and released
  read-write. Ingest mounts only quarantine; Cleanup mounts both zones.

Filesystem volumes are only a single-host reference backend. Production must
use private, versioned S3-compatible buckets with independent IAM policies. No
business database or Redis credentials are injected into the five new service
definitions.

## Configuration and health

Each process loads only its role-prefixed environment variables and rejects
missing identities, wrong audiences, malformed/non-rotatable keyrings, unpinned
Executor images, unavailable OCI/scanner engines, or inaccessible durable
storage. There is no automatic fallback to Mock or DB-backed blobs.

All workloads expose `/livez` and `/readyz`; Compose healthchecks use `/readyz`.
Runtime readiness requires writable durable state/spool and configured Ingest.
Executor readiness requires its store, a usable rootless OCI driver, and the
digest-pinned execution image. Egress readiness requires its durable action
store and explicit enablement; search is advertised only with a configured
provider. Scanner readiness requires its policy engine, work directory,
operation store, and versioned storage.

Production entrypoints also require the candidate service image digest and source
SHA. `/livez` exposes those bindings, and the release preflight checks Runtime,
Executor, Ingest, Scanner, and Cleanup against both the tested SHA and the exact
D0 service digest set. It probes both `/livez` identity and successful `/readyz`
for all five services. This is evidence input, not permission to self-attest:
the deployment platform must bind each value to the actual immutable workload
image.

Artifact receipts use distinct Ed25519 issuers. Each Artifact service receives
only its current read-only private PEM; API/Runner receive only the distinct
current/next public PEM trust set. Global admission and release preflight reject
HS256 Artifact receipts. The production orchestrator must mount these files and
the protected D0 release-check receipt outside the worktree. The reference
Compose graph binds operator-supplied host paths to fixed container paths as
read-only mounts. Its `/dev/null` defaults are deliberately invalid in enabled
production mode: the graph neither generates keys nor manufactures attestation.

Production secrets must be injected by the workload orchestrator. They must not
be placed in the shared `deploy/backend/.env`, because the legacy Runner still
loads that file. The Compose `${...}` references demonstrate variable ownership
only; they are not a production secret manager.

## Production gate

The checked-in D0 record remains
`BLOCKED_PENDING_EXTERNAL_ATTESTATION` and `approval_eligible=false`. It is a
request, not an approval. Production remains disabled until an authorized
external system binds all of the following to protected evidence:

- digest-pinned Runtime, Executor, Ingest, Scanner, and Cleanup images;
- per-workload service accounts, mTLS identities, JWT audiences, and receipt
  verification trust roots;
- exact network policy and topology hashes;
- a qualified dedicated rootless Linux OCI host;
- approved scanner engine/image and private versioned object-store IAM;
- staged canary, rollback, capacity, and isolation evidence.

The current D0 request predates the restricted-Egress workload and the bounded
Executor-to-Ingest edge. Functional canary testing may run with global admission
disabled, but production review must issue a refreshed request covering those
identities, images, secrets, and network policies.

The shared backend image in Compose does not contain an OCI CLI or malware
scanner. Therefore Executor and Scanner correctly remain unready unless an
approved role-specific image/runtime supplies them. This fail-closed behavior is
intentional and must not be weakened with placeholder binaries.

Runner startup probes Runtime, Executor, and the aggregate Artifact client, whose
readiness requires Ingest, Scanner, and Cleanup. The release gate separately
probes all five `/livez` and `/readyz` endpoints, requires EdDSA Artifact
receipts, and rejects any tested-SHA or D0 image-digest mismatch. The request
still contains unresolved external fields, so these code-level checks cannot
authorize a release today.

## Rule

A deployment that collapses identities, shares downstream secrets, admits
traffic while a dependency is unready, or lacks the protected D0 evidence
cannot claim production capability. All production feature flags stay false in
that state.
