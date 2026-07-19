# Lab Phase 8 staging drills — real Postgres + Redis

Recovery plan Phase 8 (deployment smoke + operational drills), run 2026-07-19 on
the dedicated Linux runner (`100.93.72.102`, Oracle Cloud aarch64, Ubuntu 22.04,
rootless Docker 29.2.1) against **real infrastructure** — `pgvector/pgvector:pg16`
Postgres and `redis:8-alpine`, both as rootless containers — NOT SQLite/fakeredis.

## 1. Alembic migrations apply on REAL Postgres

`alembic upgrade head` → `036_add_outbox_dispatch (head)`, exit 0. The full
migration chain (through the recovery plan's new `036`) applies cleanly on real
Postgres; `outbox_events` gains `dispatch_status / attempts / next_attempt_at /
locked_until / last_error` + the `ix_outbox_events_dispatch_status` index. This is
the "verify on real Postgres before deploy" the migration files require — the
local suite only exercises SQLite `create_all`.

Real Postgres FK enforcement also caught a seed-data bug SQLite silently accepted
(`residents.creator_id` referencing a missing `system` user) — evidence the drill
exercises constraints the unit suite does not.

## 2. Deployment smoke — the real Lab Runner consumes the queue

Seeded a funded task (enqueues a run), then ran the ACTUAL
`python -m app.lab.main` Lab Runner process for 15s against Postgres+Redis:

    TASK = review   RUN = succeeded   issuer_balance = 890   queue_depth 1 -> 0

The runner consumed the queued run and drove the full pipeline
(funding → run → mark_review) on real infra; the escrow (1000 − 110 =
reward 100 + fee 10) is correct and the queue drained. The loop's periodic
concurrency-reconcile + SLO snapshot ran without error.

## 3. Runtime kill-switch drill

- Kill switch ENGAGED (`sv:lab:enabled=0`): runner ran 8s → `TASK=assigned,
  queue_depth=1` — the run was NOT consumed (requeued).
- Kill switch DISENGAGED: runner ran 12s → `TASK=review, RUN=succeeded,
  queue_depth=0` — consumed and executed.

The runtime kill switch blocks queue consumption and cleanly resumes it.

## Files

- `staging-summary-*.txt` — consolidated posture + results.
- `staging-drills.txt` — kill-switch drill raw output.

## Honest boundary

These prove the deployment topology (migrations + runner + queue + kill switch)
on real Postgres/Redis with the Mock adapter. They do NOT enable a real Adapter
(P7 blocked — no runtime endpoint) or `lab_oci_enabled` (that needs the separate
OCI canary; V11 isolation evidence is in `../lab-oci-evidence/`). Chaos/capacity/
rollback drills against a full multi-service staging cluster with least-privilege
identities remain.
