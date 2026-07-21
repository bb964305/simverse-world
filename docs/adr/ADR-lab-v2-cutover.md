# ADR: Lab protocol-v2 cutover and single-writer enforcement

- Status: Accepted for D1a feasibility; P1 and default-off P2 subsets verified;
  D1b overall and D1c pending
- Decision date: 2026-07-21
- Plan authority: `lab-agent-blocker-resolution-plan.md`, Approved v10
- Baseline: `feat/lab-agent-v1@77b64c2878a1adeba7a44c8844a19ed9fa642d26`

## Decision

Proceed with the approved A' hybrid. Task, Hold, Artifact, Broker, Ledger, and
world-governance data remain one business domain. Protocol-v2 execution receives
separate session/turn/intent/result/control state and physical queue keys. All
Lab financial terminal effects converge on one controlled entrypoint owned by a
NOLOGIN role and callable only by the Lab Runner's dedicated terminalizer role.

This D1a decision authorizes default-off P1-P4b implementation only. It does not
approve production services, images, network policy, canary traffic, or release.

## D0 status

D0 is `BLOCKED_PENDING_EXTERNAL_ATTESTATION`. The request-only record is
`.omx/approvals/lab-agent-services-d0.json`; its canonical request hash is
`e8cbab7eeaf38c91b24dc80da137b6164ebec098e6b55978de366f1cf6732660`.
All image and production topology digests remain unresolved. No attestation,
trust root, approver policy, signature, or approved scope exists in the agent's
authority domain. P5, production service/image/network changes, and P7 remain
blocked.

## D1a evidence

The immutable dirty baseline is outside the worktree at
`/Volumes/data/dev/simverse-world-release-evidence/pre-implementation/dirty-manifest.json`.
It records 20 pre-existing paths and the raw porcelain-v2 digest
`0ef6f17918407c73f8ce02aadcfe722ff3b16304f111e3aaa917d516dea01b04`.

`backend/scripts/audit_lab_terminal_writers.py --strict` produced these results:

| Hard oracle | Result | Evidence |
|---|---|---|
| Source/runtime writer inventory has no unknown | PASS | 37 exact direct, aliased, dynamic, constructor, and SQL call/write findings; zero missing and zero unknown |
| Controlled Postgres entrypoint can reject legacy/direct mutation | PASS | PostgreSQL 16.14; legacy DML, terminalizer direct DML, PUBLIC function call, and `SET ROLE` all denied |
| v1/v2 queues can be physically isolated | PASS | Dedicated Redis 7.4.9 spike; v1 second claim was empty while v2 retained its own run |
| A' avoids core-domain duplication | PASS | One financial domain; no duplicate Task/Hold/Ledger/Broker tables |

The raw Postgres/Redis spike is external and read-only at
`/Volumes/data/dev/simverse-world-release-evidence/pre-implementation/d1a-feasibility-spike.log`
with SHA-256
`3e466a8b5d82afa0eb1bf8efe1776016488eb6dbc4ee4836a14d0902fb54268e`.
The current structured writer inventory is external and read-only at
`/Volumes/data/dev/simverse-world-release-evidence/pre-implementation/d1a-writer-inventory-v2.json`
with SHA-256
`1d4bede3524ba9ff01ee2c21914683fa155f494a1433e71d411b1aaca31ffb4a`.

The current runtime writer topology is intentionally treated as unsafe input to
the cutover, not as the target state:

| Process/path | Current terminal reachability | P1 target |
|---|---|---|
| API/player/admin routes | accept, cancel, admin cancel | append an authorized durable command only |
| API optional nightly cron | auto-release, expire, orphan failure | append scheduler commands only |
| agent-worker nightly cron | auto-release, expire, orphan failure | append scheduler commands only |
| Lab Runner | run success/failure and kill | sole controlled-entrypoint caller after fencing proof |
| `coin_service.settle/refund` | direct Hold/balance/treasury writes with commits | compatibility wrappers excluded from Lab paths |
| `transitions.cas_task_status` | generic Task status update | nonterminal transitions only outside terminalizer |

There is no configured dynamic SQL terminal job. The source audit catches direct,
aliased, dynamic, constructor, `setattr`, and SQL keyword status writes and fails
when a newly detected site lacks classification. It is defense-in-depth, not an
authorization mechanism; D1b's real Postgres grants and legacy-role probes remain
the enforcement oracle.

## P0 regression baseline

The behavior-first suite collects 52 new cases. Against `77b64c2`, the 42 local
cases produce 27 expected failures, seven missing-surface setup errors, and eight
passes for already-present world atomicity/helper behavior. Ten integration cases
with their required switches asserted and infrastructure inputs withheld all exit
nonzero rather than skip. The immutable JUnit bundle and manifest are at
`/Volumes/data/dev/simverse-world-release-evidence/p0-expected-red/`; its manifest
SHA-256 is `c14b72af2ddb1bb0d8e4180d2deb7ad13356b8ae93286dfe6bfca357d865fc1e`.

## P1 financial and database evidence

P1 is implemented default-off through additive migration
`038_add_lab_terminalization_v2`. The database kernel owns Task, Hold, journal,
balance, treasury, receipt, and terminal outbox effects in one transaction. Its
owner is the NOLOGIN `lab_financial_kernel_owner`; `lab_terminalizer_v2` has
only controlled-entrypoint execution, and the NOLOGIN breakglass role has no
standing execution grant. Breakglass activation uses a separate balanced
compensation ledger and immutable audit/outbox records.

The real-Postgres suite has 17 cases. It includes a 100-round SQL
settle-versus-refund race, service-level exact retry and fencing, bounded retry,
concurrent single-owner safety fencing, opposing lock order, SQL and Python
fault matrices, failure-recorder row-lock convergence, role and direct-DML denial,
breakglass activation/revocation and fault rollback, and downgrade refusal.
Fresh-schema upgrade reached revision 038; a separate clean database downgraded
to revision 037; any command, receipt, journal, v2-hold, or compensation history
makes downgrade fail before schema mutation. Focused completion-worktree
verification currently covers 110 local P1 cases plus the 17-case real-Postgres
suite.

Machine evidence is outside Git at
`/Volumes/data/dev/simverse-world-release-evidence/p1-terminalization/`:

| Evidence | Result | SHA-256 |
|---|---|---|
| `writer-inventory.json` | 45 findings; zero missing/unknown; all hard oracles pass and A' is retained | `ea41596c210b72911220c13e9a6a0c1ad81a3739822dec87e2e75308c230667e` |
| `cohort-matrix.json` | 1120 unique tuples; every tuple has one rule/action; seven rows collected directly from a disposable migrated PostgreSQL database map with zero anomalies or unresolved rows | `5b2d293423d093c873fc99ae52b6843a19b93eb1a2ffa8ed6fe6d4c812d38fa3` |
| `finance-reconciliation.json` | Fresh migrated disposable PostgreSQL database; two tasks, two holds, five journal entries, one completed v2 command/receipt; read-only reconciliation with zero anomalies | `7d92b49662b29aff42a155c92b9ccb2ab5ee46c721d6a4942866d61dadb22a96` |

This evidence closes only the P1 financial/DB part of D1b. It does not prove
physical queue isolation, session durability, fleet-wide credential placement,
or absence of old writers in a deployed fleet. D1b overall therefore remains
pending, and all rollout flags remain false.

## P2 protocol, state, and Runtime boundary evidence

P2 is implemented default-off through additive revision
`039_add_lab_protocol_v2_state`. Historical runs are backfilled to protocol v1;
new rows must explicitly choose strict integer version 1 or 2. An ORM guard,
database check, and PostgreSQL trigger make the version immutable after insert.
The migration refuses malformed or preclaimed enqueue history, and its downgrade
locks the affected tables and refuses any v2 run, Runtime, control, or enqueue
history before changing schema.

Protocol ownership is now physical rather than advisory:

- v1 uses `sv:lab:v1:{queue,processing}` and v2 uses
  `sv:lab:v2:{queue,processing}`; the two legacy unversioned lists must be empty
  before a Runner starts;
- enqueue outbox payloads bind `run_id` and `protocol_version`, and the dispatcher
  rejects envelope/payload mismatches before publishing;
- only a v1 consumer handler is registered in P2. A v2 Runner or run fails before
  child tasks, holds, run state, outbox, Redis, or Mock/v1 execution until P3
  installs the real result-loop handler;
- v2 admission requires the exact `simverse_ref` adapter. Mock and every other
  adapter fail closed rather than becoming a fallback.

Revision 039 owns durable session, turn, intent, result, and control state. Its
composite foreign keys bind each intent to one session/epoch and turn/session,
and bind every result to the exact session, turn, intent, action, and epoch.
Negative PostgreSQL probes reject every cross-binding variant. Provider session
creation registers and commits `creating` before external I/O, uses a deterministic
client id, and requires idempotent create plus reattach. The caller must hold the
exact live run lease. Final ready/verify transitions lock lease then session,
recheck PostgreSQL wall-clock expiry, retain a live-lease CAS predicate, and
rollback before cleanup on every exception. Same-host process restart reattaches
the durable session; host/volume loss or a divergent locator quarantines it.

The protocol-v2 Runtime stores bounded model/session state and command receipts
in a hardened SQLite file. Every `/runs/**` route authenticates a short-lived,
run/session/epoch/action-scoped JWT before session lookup; current and next keys
are accepted for the dedicated `lab-runtime` audience, while expired, malformed,
wrong-audience, wrong-action, and cross-binding replay tokens are rejected. Exact
valid retries return the original durable receipt. Importing `module:app` exposes
no run route, and the supported standalone entrypoint requires an explicit
protocol plus complete durable-store/keyring configuration. Production TLS,
mTLS identity, service deployment, and volume operation remain P5/D0 work.

Fresh disposable PostgreSQL evidence reached the single 039 head: three migration
tests and ten Runtime durability/concurrency tests passed. The latter includes
create-before-provider crash recovery, host-loss quarantine, owner/epoch takeover,
lease-row and session-transition expiry waits, final-CAS fencing, and lock-release
proofs. The current local protocol/auth/state suites and the P1-on-head compatibility
group are green. External P2 command logs and hashes are stored under
`/Volumes/data/dev/simverse-world-release-evidence/p2-protocol-session/`.

This closes the P2 physical-queue/session-durability portion of D1b only.
Fleet-wide credential placement and legacy-writer absence still require P5 after
a valid D0. P3 result delivery, provider ACK/replay, Artifact provenance, and
Runner ACK semantics are explicitly not claimed by this section.

## Comparative evidence

Counts are derived from explicit file/symbol/table/backfill/service lists emitted
by the audit script, not from prose estimates:

| Option | Files | Symbols | New tables | Backfills | New operated services | Financial domains |
|---|---:|---:|---:|---:|---:|---:|
| A' hybrid | 28 | 29 | 12 | 3 | 3 | 1 |
| B isolated domain | 38 | 39 | 20 | 8 | 5 | 2 |

A' meets every Approved-v10 comparison rule: it retains one financial kernel,
duplicates zero core-domain tables, operates no more services than B, and has
fewer migration/backfill steps. B remains the mandatory fallback if D1b proves
that legacy mutation cannot be revoked, a cohort is unknown, physical queue
cross-claim is nonzero, or the declared session durability class fails.

## Planned enforcement

- `lab_financial_kernel_owner`: NOLOGIN owner, no runtime credential.
- `lab_terminalizer_v2`: LOGIN only in the Lab Runner terminalization component;
  controlled-entrypoint EXECUTE and no direct table DML or owner membership.
- `lab_terminalizer_breakglass`: NOLOGIN and ungranted by default; separate
  externally approved compensation entrypoint with append-only audit.
- Queue keys: `sv:lab:v1:{queue,processing}` and
  `sv:lab:v2:{queue,processing}`. Cross-protocol claims are invalid.
- Rollout flags remain independently false. A v2 run never falls back to v1 or
  Mock, and an old binary is never a rollback target.

## Remaining gates

- D1b: P1 real-Postgres financial grants and the complete 1120-row cohort matrix,
  plus P2 immutable protocol version, physical queue isolation, strict consumer
  routing, scoped Runtime auth, and measured session-affine durability are
  verified default-off. P5 must still prove fleet-wide role placement and
  legacy-writer absence after a valid D0.
- D1c-control/dispatcher subset: Runtime and Executor control receipts, nominal
  and fault global-kill drills, and per-owner outbox deny matrix.
- D1c overall: P5 identity/topology evidence after a valid external D0 approval.
