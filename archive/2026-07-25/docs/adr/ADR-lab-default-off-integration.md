# ADR: Lab Protocol-v2 Default-off Mainline Integration

- Status: Accepted as a scoped amendment; candidate eligibility remains gated
- Date: 2026-07-22
- Scope: repository integration through Approved-v10 P4b only

## Context

Approved-v10 correctly blocks production services, identities, network policy,
enablement, release evidence, and release push while D0 lacks protected external
attestation. It also places every merge under P7, which prevents the repository
from integrating a complete, disabled P0-P4b implementation independently from
production authorization.

That coupling is unnecessarily broad. A default-off integration can be reviewed
and reverted as source code without granting any production capability, provided
the code boundary is complete and the tested commit cannot activate itself.

## Decision

The default branch may accept a Lab protocol-v2 integration checkpoint before
D0 only when all of the following are true at one clean commit:

1. P0-P4b implementation gates pass, including result/ACK recovery, durable
   control, Runtime and Executor fanout, v2 processing reclaim, trust-plane
   outbox ownership, and atomic/fenced world apply and revert.
2. `lab_agent_v2_enabled`, `lab_terminalizer_v2_enabled`,
   `lab_outbox_v2_enabled`, `lab_runtime_v2_canary_enabled`, and
   `lab_global_admission_enabled` all default to false.
3. The Alembic graph has one head; migrations are additive and retain the
   documented fail-closed downgrade boundary.
4. Focused merge evidence is generated from and names the clean integration
   commit. Documentation must distinguish subset readiness from production or
   release approval.
5. The diff contains no P5 production service, image, identity, secret, network,
   or deployment activation change.

This checkpoint is a source integration decision, not Approved-v10 P7 release.
It does not satisfy D0, D1c overall, AC14-AC17, AC19-AC21, staging, capacity,
visual, asset, production topology, or release review gates.

## Consequences

- D0 remains the sole authority for production topology and enablement.
- Every protocol-v2 rollout flag remains false after integration.
- P5 and later work must continue from a new, current D0 attestation whose scope
  and digests match the deployment candidate.
- A red P0-P4b gate, dirty tested tree, unresolved migration head, or production
  deployment diff makes the candidate ineligible for this amendment.
- Mainline integration must use the normal review workflow and must not be
  described as a release, canary, or production approval.

## Candidate Verification (2026-07-22)

The candidate satisfies the scoped source-integration boundary:

- Alembic upgrades an empty PostgreSQL/pgvector database through the single
  `042_lab_world_fencing` head; 042 downgrades to 041 and upgrades again. The
  new epoch and uniqueness constraints are present in the resulting schema.
- Focused deterministic gates pass for P3 budgets/result delivery/supervision,
  P4 control/outbox/queue recovery/authentication, P4b atomic world governance,
  rollout guards, release-manifest ownership, and the Lab end-to-end sentinel.
- Required integration cases pass against disposable real PostgreSQL and Redis,
  including commit-before-ACK recovery, global control, v2 delivery reclaim,
  concurrent proposal approval, and outbox-fault rollback.
- All five protocol-v2 rollout settings default to false. The branch's earlier
  Lab Runner Compose scaffold is excluded from the default graph by an opt-in
  `lab` profile and remains dormant unless `LAB_ENABLED` is also enabled; its OCI
  helper only collects evidence. The completion delta's sole deployment change
  makes that existing Runner explicitly opt-in; it adds no frontend change or P5
  Runtime/Executor service, image, production identity, network policy, secret,
  or activation.

The repository's historical ORM/Alembic metadata drift still makes
`alembic check` report unrelated legacy differences. This is not treated as a
green schema-parity signal: the candidate relies on the clean migration chain,
single-head check, direct 040-042 schema inspection, and focused real-database
tests. Repairing the broader historical drift remains separate work.

The full backend/frontend suites were intentionally not rerun for this
backend-only, default-off checkpoint. Verification is limited to the affected
phase gates and integration boundaries; production and UI claims remain outside
this decision.

## Supersession Boundary

This ADR narrows only the Approved-v10 statement that all merge activity waits
for P7. It does not change any D0 constraint or any P5-P7 release requirement.
