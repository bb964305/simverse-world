# ADR: Lab protocol-v2 cutover and single-writer enforcement

- Status: Accepted for D1a feasibility; D1b and D1c pending
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

## Comparative evidence

Counts are derived from explicit file/symbol/table/backfill/service lists emitted
by the audit script, not from prose estimates:

| Option | Files | Symbols | New tables | Backfills | New operated services | Financial domains |
|---|---:|---:|---:|---:|---:|---:|
| A' hybrid | 28 | 29 | 11 | 3 | 3 | 1 |
| B isolated domain | 38 | 39 | 19 | 8 | 5 | 2 |

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

- D1b: real Postgres grants/legacy probes, complete 1120-row cohort matrix,
  immutable protocol version, physical queue cross-claim zero, and measured
  session-affine durability.
- D1c-control/dispatcher subset: Runtime and Executor control receipts, nominal
  and fault global-kill drills, and per-owner outbox deny matrix.
- D1c overall: P5 identity/topology evidence after a valid external D0 approval.
