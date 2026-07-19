# T8 — P5 Hardening

Session 2026-07-19 (Cowork). Covers the feasible P5 deliverables (telemetry
taxonomy + emitter, retention/cleanup drill evidence, kill/rollback runbook,
production-isolation evaluation) and marks the infra-dependent ones
(chaos/capacity, staging drills) as blocked with a plan.

## 1. Content-free telemetry & alerts (implemented)

`app/lab/telemetry.py` — a content-free-**by-construction** alert emitter. It
accepts only an allowlist of structural fields (ids, dimension names, reason
codes, counts, hashes) and raises `TelemetryLeak` if a content-bearing field
(`summary`/`content`/`payload`/`text`/`args`/`thought`/…) is passed, so
hard-constraint #3 (raw thought/content never enters logs/telemetry) is enforced
at the call site rather than trusted. Emission is best-effort (never raises into
a security path). Tests: `test_lab_telemetry.py`.

Alert taxonomy (`LabAlert`, the 7 conditions the deploy spec requires):

| Alert | Fires when | Wire point |
|---|---|---|
| `budget_exhausted` | a budget dimension hits its limit → grant revoked, run stopped | **wired**: `budgets._exhaust` |
| `world_apply_failed` | a world proposal apply/revert fails | **wired**: `proposal_service._fail_apply` |
| `orphan_heartbeat` | a run's lease lapses (no heartbeat past TTL) → reap+refund | follow-up: lease reaper |
| `stale_epoch` | a fenced (pre-takeover) writer is rejected | follow-up: `broker`/`leases` StaleEpoch path |
| `blocked_egress` | an egress target outside the allowlist is denied | follow-up: `broker._validate_egress` denial |
| `approval_timeout` | a sensitive-action approval expires → default deny | follow-up: approval-timeout sweep |
| `cleanup_quarantine` | a workspace/artifact can't be cleaned → quarantined | follow-up: artifact cleanup path |

Two high-signal sites are wired now (budget exhaustion, world apply failure)
proving the pattern; the remaining five are single best-effort `emit_alert`
calls at the listed sites (each carries only ids + a fixed reason code — never
the raw failure text). They are additive and non-security-invariant.

## 2. Retention / cleanup evidence (existing, drill = green)

V12 retention is implemented and covered by `test_lab_retention.py`:
tombstone on destructive cleanup (flag-gated), `retention_hold` pins evidence
(held accepted/proposal/revision artifacts are not swept), tombstone ordering,
and quarantine of a workspace that fails to clean. Drill: run
`pytest tests/test_lab_retention.py tests/test_lab_artifacts.py` — green this
session. Production drill additionally records a real quarantine directory +
tombstone row on the staging runner.

## 3. Staging kill / rollback runbook

1. **Kill switch.** `POST /admin/lab/kill-switch` (admin auth) flips the runtime
   flag; `is_lab_runtime_enabled()` returns False so `/lab/tasks` publish is
   503. In-flight runs: `POST /admin/lab/runs/{id}/cancel` → cancel escalation
   (TERM→KILL within the 10 s window, `test_lab_runtime_contract`).
2. **Revoke grants + fence.** `grants.revoke_run_grants(run_id)` (or
   `revoke_grants_before_epoch` on takeover) revokes every live grant; the
   Broker denies any further tool call (`grant revoked`/`stale epoch`).
3. **Kill sandboxes.** OCI executor teardown is verified (`--rm` + inspect); a
   container still present marks the executor unusable. Confirm no orphan
   containers on the runner.
4. **World rollback.** For any applied proposal, `revert_proposal` restores the
   captured before-state and emits a `world_changed` reverted envelope; main
   map / minimap / Codex reconverge at the common `world_revision_id`.
5. **Verify.** `alerts` show the kill/rollback; no unbrokered egress or
   filesystem event without a matching current-epoch action id.

## 4. Production-isolation candidate evaluation (gVisor / Kata / Firecracker)

The current rootless-OCI executor (`oci_executor.py`) gives dev-grade isolation
on colima and correct guarantees on a real Linux runner (V11). For production,
evaluate a stronger sandbox under the same `docker run`-shaped argv:

| Candidate | Isolation model | Pros | Cons / cost | Fit |
|---|---|---|---|---|
| **gVisor (runsc)** | user-space kernel intercepting syscalls | drop-in OCI runtime (`--runtime=runsc`), no VM, strong syscall containment | ~10–30% syscall overhead; some syscalls unimplemented | **Recommended first** — smallest change to the existing executor; set the runtime flag, keep the argv |
| **Kata Containers** | lightweight VM per container (hardware virt) | true kernel isolation, OCI-compatible | needs nested virt / bare-metal; higher memory/boot cost | Strong isolation if the runner supports virt |
| **Firecracker** | microVM (KVM) | minimal microVM, fast boot, used by Lambda/Fargate | not a drop-in OCI runtime (needs a shim like firecracker-containerd); more integration work | Best at scale, most integration effort |

Recommendation: pilot **gVisor** on the dedicated Linux runner (add
`--runtime=runsc` to `build_run_argv`, gate behind a config flag), re-run the
`lab_oci` adversarial suite, and record syscall-overhead + escape-attempt
evidence. Escalate to Kata/Firecracker only if the threat model requires
hardware-level isolation. Decision stays open pending the runner (see
`deploy/lab-oci-runner/`); `lab_oci_enabled` remains False until then.

## 5. Chaos / capacity (blocked — needs infra)

Requires separate service identities/processes + a staging cluster (PRD
§Deployment: API + agent-worker are the only defined services today). Planned:
concurrent-run capacity ramp to the 3-worker cap × N runs, egress-proxy
fault injection, Redis/DB failover during a run (verify budget counters +
reservations survive resume), and the E-13 concurrency-cliff cost model. These
run on staging, not in this sandbox.
