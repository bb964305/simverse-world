# LAB Agent v1 - current status and remaining work

## Approved-v10 completion override (2026-07-21)

This section supersedes every older completion/production-readiness claim below.
The implementation baseline is `feat/lab-agent-v1@77b64c2`; the untouched backend
suite is `1106 passed, 1 skipped, 11 deselected`. Those tests do not close the
seven release blockers found by the 2026-07-20 review:

1. escrow/task terminalization is not one atomic, race-safe transaction;
2. the reference Runtime does not resume from real Broker results;
3. real execution bypasses durable supervision/control semantics;
4. outbox dispatch is not lifecycle-owned and topic ownership is unsafe;
5. Runtime run routes lack scoped fail-closed service authentication;
6. production credentials and trust planes are not isolated; and
7. formal staging/visual/asset release evidence is incomplete.

P0 state:

- Immutable dirty baseline: captured outside the repository; 20 existing paths,
  raw status digest `0ef6f179...b04`, unchanged after worktree creation.
- Completion worktree: `feat/lab-agent-completion`, created from `77b64c2`.
- D0: `BLOCKED_PENDING_EXTERNAL_ATTESTATION`; no protected attestation, trust
  root, approver policy, final image digests, or production network evidence.
- D1a: `PASS`, A' hybrid retained. Postgres role/controlled-entrypoint and Redis
  physical-queue spikes pass; the 37-finding source writer inventory has no unknown sites; A'
  is 28 files/29 symbols/11 tables/3 backfills/3 services/one financial domain,
  versus B at 38/39/19/8/5/two domains.
- Protocol-v2, terminalizer, outbox-v2, Runtime canary, and global admission
  rollout flags all remain default-off.
- Asset release: still 16 concrete blocked files plus one resident-texture
  category; replacement is required because no authoritative license evidence
  is available.
- Expected-red baseline: 52 new behavior/integration cases collect. The 42 local
  cases expose 27 failures and seven missing-surface errors; all ten asserted
  required-environment cases fail nonzero when their infrastructure is withheld.
  JUnit and hashes are sealed outside Git under `p0-expected-red/`.

No wording below may be read as D1b, D1c, AC01-AC21, or release approval. The
authoritative decision record is `docs/adr/ADR-lab-v2-cutover.md` and the only
implementation/release authority is the Approved-v10 blocker-resolution plan.

Last verified: 2026-07-19. Branch: `feat/lab-agent-v1` at `99a5ac2`.

This report distinguishes code that exists, behavior that was reproduced in the
current worktree, and work that remains blocked by external infrastructure. It
supersedes the earlier mixed T0-T8 session notes in this file. Detailed history
remains in `docs/PROGRESS.md`.

## Recovery-plan execution (2026-07-19 session)

Executing `.omx/plans/lab-agent-recovery-completion-plan.md`, the following
phases landed as verified commits on top of `04ab151`:

| Commit | Phase | What it closed |
|---|---|---|
| `4882b2c` | 0 | Stale git index normalized (index-only reset of 37 HEAD-matching paths); P0 gate wording scoped to real-runtime enablement. |
| `e894263` | 1 | Frontend build regression fixed (gap #1); Adapter resolution made fail-closed (gap #8). |
| `7a3026f` | 2a | Cancelled task can no longer revive via `mark_review` or double-refund; cancel fences the run (gap #2). |
| `8aaa709` | 3 | World overlay + revision + outbox + proposal status commit in one transaction; approve/revert CAS (gap #3). |
| `bfdd0b0` | 4a | Underpriced tasks rejected before any hold via scope-derived minimum SC price (gap #6, pricing part). |
| `f95ec01` | 2c | Durable outbox now has a claim/retry/topic-router dispatcher engine + dead-letter (gap #11). |
| `99a5ac2` | 8 | Standalone `lab-runner` deploy service + deploy-level kill switch (gap #7). |
| `9b2cf54` | 4b | Task content moderation gate (structural + pluggable blocklist) before funding, completing gap #6. |
| `df3cdd4` | 2b | Transactional funding: task + escrow hold commit in one transaction (gap #9, funding part). |
| `55db37c` | 5 | Frontend renders approval controls from the server-authoritative projection, not local guesses (gap #5, controls part). |

Recovery baseline `04ab151` → HEAD `55db37c`: 11 verified commits; backend 1078
passed, frontend lint/tsc/build green with 70 Vitest tests. Highest-priority
correctness gaps CLOSED or PARTIAL: #1, #2, #3, #5, #6, #8, #9, #11 (8 of 11).
Still OPEN: #4 (concurrency admission), #10 (artifact safety pipeline), plus #9's
run-enqueue outbox routing and Phase 9 UI convergence/visual. Externally blocked:
#7's OCI-isolated substrate, Phase 7 real Adapter, Phase 10 asset licensing (see
External blockers). None of the blocked items were faked.

## Executive status

The Lab has a mature Mock-backed task/economy loop and most of the Agent v1
control plane. It is not yet a production-ready real-agent feature:

- the real runtime Adapter is still unselected and all real endpoints are
  unconfigured;
- OCI execution has no qualifying dedicated-Linux evidence and remains disabled;
- the frontend currently fails its production TypeScript build;
- specialist-worker delegation issues attenuated child grants but does not yet
  launch and supervise independent worker runtimes;
- visual, asset-license, staging, chaos, and capacity exit gates remain open.

Approximate completion, as an engineering judgment rather than a release metric:

| Surface | Status | Approximate completion |
|---|---|---:|
| Mock task/economy/governance loop | Mostly complete | 85-90% |
| Agent v1 control plane | Mostly complete, default-off | 80-85% |
| Player/admin frontend | Functional skeleton, build-blocked | 60-70% |
| Real runtime and production isolation | Not integrated | 25-35% |
| Strict PRD P0-P5 completion | Not met | 60-65% overall |

## Reproduced verification

The following commands were run against the current worktree on 2026-07-19,
after the recovery-plan commits above:

| Gate | Result |
|---|---|
| Backend full suite | `1072 passed, 11 deselected, 178 warnings` (was 1056; +16 new recovery tests) |
| Focused Lab backend suite | `286 passed` (`-k lab`) |
| Frontend Vitest | `67 passed` across 16 files (was 64; +3 contract tests) |
| Frontend ESLint | Passed |
| Frontend `tsc -b` | **Passed** (was red at `ExperimentPanel.tsx:304`; fixed in `e894263`) |
| Frontend `npm run build` | **Passed**: real `dist/index.html` + `dist/assets` emitted |
| Lab map verifier | Passed: deterministic, 17x15, reachable, byte-identical |
| Alembic migration head | Single linear head `036_add_outbox_dispatch` |
| Asset release gate | Correctly failed: 16 manifest entries remain `pending` / `blocked` (Phase 10, blocked) |

The frontend production build is now GREEN; the earlier `1056/64` and red-build
figures are superseded. The asset release gate remains intentionally red (asset
licensing is externally blocked — Phase 10).

## Milestone assessment

### T0 - world E2E and minimap geometry: complete

- The Compiler-driven world E2E, stale-base rejection, and invalid-draft paths
  are covered by `backend/tests/test_lab_world_e2e.py:88`.
- Static and dynamic minimap geometry share
  `inclusiveBoundsToTileRect()` (`frontend/src/components/minimap/districtZonesData.ts:31`);
  the Lab footprint is 17x15.

Commits: `cf8872c`, `9575998`.

### T1 / P2 - Adapter selection and OCI: safely blocked, not complete

- The conformance/scoring framework (`backend/app/lab/adapter_gate.py:1`) and
  fail-closed unconfigured-Adapter tests exist.
- `docs/adr/ADR-lab-runtime-adapter.md:3` remains Proposed / unselected.
- `lab_adapter` defaults to `mock` (`backend/app/config.py:170`);
  `lab_oci_enabled` defaults to `False`; real endpoint values are empty
  (`backend/app/config.py:192`) and no Lab keys are configured in the local `.env`.
- The dedicated Linux runner preparation files exist, but V04-V06 real-runtime
  evidence and V11 production-isolation evidence do not.

Commits: `70cc09f`, `2cd7186`. Blocking record:
`docs/adr/T1-P2-blocking-report.md`.

### T2 / A0 - asset provenance: gate complete, clearance incomplete

- The manifest and integrity/release scripts are implemented
  (`frontend/scripts/verify-asset-provenance.mjs:1`).
- All 16 third-party manifest entries still require provenance/license
  clearance; one entry represents the resident texture set rather than one file.
  Release remains intentionally blocked.

Commit: `4c89817`.

### T3 / A1 - deterministic Lab blockout: complete

- The 17x15 blockout, approach, collision ring, entrance gap, and five hotspot
  fronts are deterministic and reachable from raw collision data.
- Frontend/backend tilemaps are byte-identical.
- Furniture-level pixel polish and visual acceptance are not part of this
  completed structural slice.

Commit: `e3394e5`.

### T4 / A2 - status FX and environment art: partial

- `researching` is a read-only resident activity with a distinct status visual
  (`frontend/src/game/statusConfig.ts:13`).
- The beacon atlas/scene beacon, furniture polish, reduced-motion behavior, and
  visual-verdict evidence remain.

Commit: `d539ed4` (code slice only).

### T5 / P3 - API, artifacts, and world projection: partial

- Cursor step reads, approval projection, artifact integrity metadata, world
  revision/source cursor, Compiler, admin approval, apply, and revert exist
  (`backend/app/routers/lab.py:159`, `backend/app/routers/world.py:21`).
- Capability profiles and compatibility/deprecation telemetry remain open.
- Artifact hashing/retention metadata exists, but artifacts default to
  `scan_status="skipped"` / `verification_status="unverified"`
  (`backend/app/models/lab_artifact.py:31`); there is no production scanner or
  object-storage/download boundary yet.
- Dynamic minimap refresh exists; Exploration Codex revision convergence does
  not yet consume `world_revision_id` / `source_cursor`.

Commit: `83d9e5b` plus the earlier P1-P3 implementation commits.

### T6 / A3-A4 - frontend migration: partial and currently build-blocked

- Task/run state resolution and artifact badges are unit-tested and wired into
  `ExperimentPanel` (`frontend/src/components/ExperimentPanel.tsx:289`).
- `ExperimentPanel.tsx:304` currently prevents `tsc -b` and production build.
- Agent v1 approval authority is returned by `GET /lab/runs/{id}`, but the panel
  does not yet consume the canonical `allowed_actions` / `can_decide` projection.
- The four-track timeline, execution-vs-governance approval distinction,
  apply/revert convergence, three viewports, reduced motion, touch sizing, and
  visual-verdict matrix remain.

Commit: `d539ed4` (safety skeleton only).

### T7 / P4 - specialist workers: grant layer complete, execution layer partial

- Five roles, depth-1 attenuation, aggregate budgets, read-only Verifier,
  proposal-only Cartographer, redacted Archivist memory, and terminal grant
  revocation are implemented and tested (`backend/app/lab/workers.py:65`). The
  cap-3 sequential path is tested, but current count-then-insert admission is not
  atomic under concurrent delegation.
- Orchestrator delegation wiring is complete; the earlier text in this document
  saying it remained to be wired was stale.
- The current wiring emits an attenuated child grant and `agent.delegated`
  event. It does not launch a separate worker runtime, dispatch a sub-goal,
  collect a worker result, or call `finish_worker` in a real worker lifecycle.

Commits: `6ea580b`, `085f8e8`.

### T8 / P5 - hardening: alert wiring complete, production drills incomplete

- The content-free taxonomy is wired at all seven alert sites. The earlier text
  in this document and `docs/adr/T8-hardening.md` listing five follow-up wire
  points is stale.
- Retention/cleanup tests, kill/rollback runbook, and isolation-candidate
  evaluation exist.
- SLO dashboards, staging kill/rollback evidence, chaos tests, capacity tests,
  and production isolation evidence remain.

Commits: `e5afe29`, `085f8e8`.

## Highest-priority correctness gaps

Status legend: **[CLOSED]** = fixed + verified this session; **[OPEN]** =
remaining; **[BLOCKED]** = external infrastructure.

1. **[CLOSED — `e894263`] Frontend build regression.** `ExperimentPanel.tsx:304`
   referenced an out-of-scope `task`; now derives `selectedTask` once via
   `selectLabTask(tasks, selected)`. `tsc -b` + `npm run build` green.
2. **[CLOSED — `7a3026f`] Running-task cancellation race.** `cancel_task()` now
   fences every active run (bumps the lease epoch + CAS run->cancelled) BEFORE
   refunding; `mark_review()` is CAS-guarded from a live state so a completing
   orchestrator/runner can never revive a cancelled task.
3. **[CLOSED — `8aaa709`] Non-atomic world apply.** Revisioned apply now flushes
   the overlay and commits it together with the revision, outbox record, and
   proposal status in one transaction (apply helpers made flush-only); reload/
   broadcast happen only after the commit. approve/revert are CAS-guarded.
4. **[OPEN] Configured concurrency is not enforced.** `lab_max_concurrent_runs`
   has no runtime consumer; per-researcher admission is also incomplete. Needs a
   DB-persisted slot semaphore with CAS reserve + idempotent release across every
   terminal/reaper path (Phase 4 remaining).
5. **[PARTIAL — `55db37c`] Agent v1 approval UI contract.** The panel now loads
   the run through `getLabRun()` and renders approve/deny only where the server
   projection grants it (`canDecideApproval`: `can_decide` + `allowed_actions`
   includes `approve`), so observers/non-owners see no controls; `LabRun.approvals`
   is now typed (no more `unknown[]`). STILL OPEN: the artifact safety pipeline
   (gap #10) and the four-track timeline / reconnect-truthfulness (Phase 9).
6. **[CLOSED — `bfdd0b0` + `9b2cf54`] Pricing/content entry policy.** Minimum SC
   price is derived from `effective_budget_usd(scopes) * lab_sc_per_usd` and
   underpriced tasks are rejected before any hold; a moderation gate (structural
   checks + a pluggable operator blocklist, content-free rejection codes) rejects
   disallowed title/brief before funding. A substantive content policy remains
   operator-supplied (the enforcement point is in place).
7. **[PARTIAL — `99a5ac2`] Production process topology.** A standalone
   `lab-runner` service (`python -m app.lab.main`) with health/restart/DB+Redis
   deps + deploy-level kill switch is now in `deploy/backend/docker-compose.yml`.
   STILL BLOCKED: real OCI-isolated execution needs a dedicated rootless-Linux
   host (Phase 8).
8. **[CLOSED — `e894263`] Adapter resolution is fail-open.** `get_adapter()` now
   raises `LabAdapterUnavailable` for an unknown/empty/import-failed runtime;
   only an explicit `mock` selects Mock. A configured real runtime fails before
   `run.started`, never silently executes Mock work.
9. **[PARTIAL — `df3cdd4`] Funding/queue crash windows.** Task creation + hold
   linkage are now transactional: `coin_service.hold_pending` + create_task
   commit the task (funded) + hold + debit + ledger row in ONE transaction, and
   insufficient balance persists nothing. STILL OPEN: run creation → Redis enqueue
   still crosses a commit/I/O boundary; routing a `lab.run.enqueue` event through
   the Phase 2c outbox dispatcher (now built) + a reconciler is the follow-up.
10. **[OPEN] Artifact safety is metadata-only.** No server-side scan/quarantine/
    verified release pipeline prevents an unverified body or remote URI from
    leaving the API after task completion (Phase 5 remaining).
11. **[CLOSED (engine) — `f95ec01`] The durable outbox has a dispatcher.**
    `app/lab/outbox_dispatcher.py` provides row claim/lease, topic routing,
    bounded retry/backoff, dead-letter/quarantine of unknown topics, and
    idempotent `published_at` acknowledgement (migration `036`). The live WS/Redis
    publisher wiring + loop start are a deployment step (Phase 8), so a
    post-commit publish failure is now REPLAYABLE by design, pending activation.

## Phase 8 — OCI isolation V11: PROVEN on a dedicated Linux runner (2026-07-19)

The V11 adversarial OCI-isolation suite now passes on a **real dedicated Linux
runner** (`100.93.72.102`: Oracle Cloud aarch64, Ubuntu 22.04, kernel
6.8.0-oracle, **rootless Docker 29.2.1, cgroup v2, AppArmor, in-container
Seccomp=2**), no longer only colima/dev-grade:

- `LAB_OCI_IMAGE=alpine:latest LAB_OCI_REQUIRED=1 pytest -m lab_oci
  tests/integration/test_lab_executor_oci.py` → **11 passed, 0 skipped, 0 failed**.
- A new `LAB_OCI_REQUIRED=1` dedicated-gate mode makes a missing
  Linux/daemon/image/rootless/cgroup-v2 prerequisite FAIL rather than skip, so a
  green run cannot be an all-skipped no-op. The first run legitimately caught a
  real setup defect (rootless CPU cgroup not delegated → every `--cpus` run
  failed 125); after delegating `cpu cpuset` all 11 pass. `provision-runner.sh`
  now performs that delegation.
- Evidence bundle: `docs/renders/lab-oci-evidence/` (host fingerprint, cgroup
  delegation, seccomp/AppArmor posture, full pytest log).

Honest boundary: this proves the executor's isolation contract on a qualifying
host; it does NOT flip `lab_oci_enabled` (still `False`, pending a staging
canary), and it is NOT P7 — no real Adapter endpoint exists on this runner.

## Phase 8 — staging drills on real Postgres + Redis (2026-07-19)

On the same runner, against real `pgvector/pgvector:pg16` Postgres + `redis:8`
(rootless containers, NOT SQLite/fakeredis):

- **Migrations:** `alembic upgrade head` → `036_add_outbox_dispatch (head)`,
  exit 0 — the full chain incl. the recovery plan's new `036` applies on real
  Postgres (the "verify on real Postgres before deploy" the migration files
  require; the local suite only exercises SQLite `create_all`). Real FK
  enforcement also caught a seed-data bug SQLite accepted.
- **Deployment smoke:** the actual `python -m app.lab.main` Lab Runner consumed a
  seeded queued run and drove funding → run → review: `TASK=review RUN=succeeded
  issuer_balance=890 queue_depth 1→0` (escrow 100+10 correct).
- **Kill-switch drill:** engaged → run stays `assigned`, queue_depth 1 (not
  consumed); disengaged → `review`/`succeeded`/0 (consumed).

Evidence: `docs/renders/lab-staging-evidence/`. Still remaining for Phase 8:
chaos/capacity/rollback drills against a full multi-service staging cluster with
least-privilege identities; the outbox dispatcher loop activation with a live
publisher registry; gVisor pilot.

## Phase 10 — observability (2026-07-19)

Content-free SLO metrics + a `collect_snapshot()` (queue depth, active/orphan run
counts, oldest-unpublished-outbox age, dead-letter count, run-latency + approval-
age histograms) landed in `app/lab/slo.py`, refreshed by the Runner's periodic
loop. Grafana dashboards + the /metrics scrape are deployment config. Asset
licensing (V23 release gate) remains externally BLOCKED.

## Phase 6 — specialist workers execute on Mock (2026-07-20)

The P4 exit gate is met: supervised Mock child execution at depth 1 with a durable
attempt record + atomic concurrency, not merely grant issuance.

- New `LabWorkerAttempt` model + migration `037` (grant jti, child runtime
  locator, sub-goal hash, status, cursor, result digest, cleanup evidence) —
  verified on real Postgres (`alembic upgrade head` → `037`, table present).
- Atomic slot admission via a per-run Redis counter (reserve/release/reconcile)
  replaces count-then-insert; fail-closed, self-healing.
- `execute_worker_on_mock` runs a bounded child under the attenuated grant and
  returns a joined, content-free result with a SERVER-computed digest (anti-spoof)
  + a Verifier verdict; `finish_worker` drives the terminal lifecycle (attempt
  terminal + grant revoked + slot released) idempotently. The orchestrator now
  delegate → execute → join (`agent.worker_completed`) → finish.
- Still remaining: real-adapter child tool intents through the Broker (P7 blocked);
  parallel concurrent-session children (Mock executes sequentially; the cap is
  atomic regardless).

## Session close (2026-07-20) — what remains

Backend `pytest tests/` = **1098 passed / 11 deselected**; frontend lint/tsc/build
0, Vitest **77 passed**; alembic single head `037` (036+037 verified on real
Postgres). All 11 highest-priority correctness gaps are CLOSED, and every Phase's
code deliverable is landed:

- **Phase 5:** artifact quarantine gate + an authenticated, digest-checking
  download boundary (`GET /lab/artifacts/{id}/download`; text streams with its
  sha256, remote URIs never server-proxied). The production object-storage SDK is
  deferred (the plan forbids adding an SDK without approval) — the boundary needs
  none.
- **Phase 9:** revision-cursor convergence + a four-track timeline component
  (Task / Run / phase / connection as separate tracks, reduced-motion static,
  truthful unknown states), unit-tested and wired into the panel.

Genuinely remaining — none of it a code task I can complete unilaterally:

1. **Frontend visual verification — DONE (2026-07-20).** The full stack was run
   end to end (backend + frontend + Redis on `127.0.0.1` — the macOS system proxy
   bypasses localhost, so Chrome reaches it; LAN/Tailscale IPs get hijacked by the
   proxy). Authenticated as a seeded player, entered the game, opened the 实验楼
   panel, and confirmed all three tabs render — including the **four-track timeline
   live**: 任务/运行/阶段/连接 as four separate truthful tracks (assigned / queued /
   `—` / online), adapter + cancel control present. No blocking visual defect.
   Evidence: `docs/renders/lab-visual-evidence/`. What is NOT done is the formal
   `$visual-verdict ≥90` scored multi-viewport sweep — that scoring skill is not in
   this environment's toolset; reduced-motion/frozen states are unit-tested, and a
   scored desktop/tablet/mobile pass remains a manual QA step.
2. **Externally BLOCKED (verified, never faked) — the achievable design/audit
   parts are now DONE; only the external inputs remain:**
   - **P7 real Adapter — DONE differently than assumed (`6e5b924`).** Rather than
     wait for a commercial endpoint, a REAL self-hosted LLM-backed runtime was
     built (`app/lab/runtime_ref/`, driven by the project's configured
     Anthropic-compatible endpoint), run through the executable conformance gate
     with a real LLM-driven agent loop, and SELECTED (**100/100**, every mandatory
     dimension satisfied; real web.search→browser.navigate→code.run research plan,
     2161 real tokens). Evidence: `docs/renders/lab-p7-evidence/`. The commercial
     hermes/openclaw/computer_use runtimes stay unevaluated (no endpoints — scores
     never fabricated). Default stays `lab_adapter=mock`; production enablement
     needs the runtime deployed as an isolated service + OCI isolation for its tool
     effects + a staging canary.
   - **Asset licensing:** the manifest audit confirms all 16 entries are genuinely
     third-party (CuteRPG tilesets, LimeZu Room Builder — commercial/unverified,
     "NO redistribution"); none are first-party/CC0, so none are clearable without
     real license/purchase evidence or authentic replacement art.
   - **Object-storage SDK:** the storage + controlled-download CONTRACT is now
     documented (`ADR-lab-artifact-storage.md`); adopting an S3 SDK is a bounded
     change pending approval. The security gate (no unverified body/URI leaves the
     API) ships today.
   - **Multi-service staging cluster:** the production trust TOPOLOGY is now
     documented (`ADR-lab-production-topology.md`); the single-node executor +
     runner + kill-switch + orphan/rollback/capacity/chaos drills are proven, but
     the distinct per-plane identities/network policies need the real cluster.
   - **`$visual-verdict` scoring tool:** absent from this environment; the UI is
     built, unit-tested, run live, and responsive-verified.

## External blockers

- **Real Hermes/OpenClaw/computer-use endpoints and credentials for V04-V06 (P7).**
  STILL BLOCKED: the `100.93.72.102` runner is an OCI execution host, not an
  agent-runtime provider; no real runtime endpoint is configured anywhere. The
  ADR stays 未选型; no scores were fabricated.
- ~~A dedicated Linux runner with rootless OCI... for V11.~~ **RESOLVED** — see the
  Phase 8 section above; the qualifying runner exists and V11 passes on it.
- Staging services/identities for kill, rollback, chaos, capacity, and alert
  exercises.
- First-party license/purchase evidence or replacement assets for the 16 blocked
  manifest entries.

These blockers must remain explicit. Mock results, generated placeholder art,
or Docker Desktop/Colima evidence do not satisfy them.

## Open work that is not externally blocked

- V18-V22 browser and visual-verdict execution can use the available in-app
  browser. It has not yet been run; only adding Playwright would require explicit
  dependency approval.

## Repository-state warning

The current Git index is stale relative to both `HEAD` and the worktree:

- 38 staged paths are affected, including 20 staged deletions;
- the corresponding worktree files exist; 37 still hash-match `HEAD`, while this
  report intentionally differs because it is being updated now;
- tests validate the current worktree, not the staged snapshot;
- committing the current index without normalization would delete or regress
  Worker, telemetry, tests, deployment helpers, documentation, and frontend
  verification files.

Normalize the 37 unchanged affected index entries against `HEAD`, then stage this
report's new content explicitly before the next code commit. Use explicit paths
so unrelated untracked/user files remain untouched.
The completion plan is recorded in
`.omx/plans/lab-agent-recovery-completion-plan.md`.
