# LAB_REMAINING — session status & remaining-work report

Session: 2026-07-19 (Cowork). Branch `feat/lab-agent-v1`. Executed the kickoff
queue in order; this records what landed, what is blocked, and what remains, so
the completion rule ("被阻塞项有书面阻塞报告 + ADR 记录，不以降级实现冒充通过")
is satisfied honestly.

## Completed (committed, gate-green)

| Task | Outcome | Commits |
|---|---|---|
| T0.1 | `test_lab_world_e2e.py` — world-segment E2E through the Compiler (V13/V15) incl. negative paths | `cf8872c` |
| T0.2 | Minimap lab footprint 17×15 via shared `inclusiveBoundsToTileRect()` (V22) + test | `9575998` |
| T1 | P2 wrap-up: adapter stays 未选型 (endpoints unconfigured), fail-closed guard test, blocking report, OCI Linux-runner prep | `70cc09f`, `2cd7186` |
| T2 | A0 asset provenance manifest + V23 release-packaging gate (fails-closed while unaudited) | `4c89817` |
| T3 | Deterministic Experiment Building blockout + `verify-lab-art.mjs` (V16/V17 core) | `e3394e5` |
| T5 | P3 world-snapshot revision anchor (V22) + artifact manifest fields; approval projection & cursor API already existed | `83d9e5b` |
| T7 | P4 specialist-worker role layer: depth-1 delegation, concurrency cap 3, read-only Verifier, Cartographer-never-apply, redacted Archivist memory, aggregate budget | `6ea580b` |
| T8 (partial) | P5 content-free telemetry/alert taxonomy (7 conditions) wired at 2 sites; hardening report (retention drill, kill/rollback runbook, gVisor/Kata/Firecracker eval) | `e5afe29` |

### Final verifier results (this session)

- Backend full regression: **1054 passed, 0 failed** (sandbox uses
  `DATABASE_URL=sqlite+aiosqlite:////tmp/svglobal.db` to avoid the FUSE-mount
  SQLite limitation — see PROGRESS env notes; the 2 global-engine tests pass on
  a real disk path).
- Frontend: eslint 0, `tsc --noEmit` 0, `tsc -b` 0, `vite build` rc=0 (to /tmp
  outDir — FUSE `emptyDir` unlink quirk), vitest **52 passed**.
- Map: `verify-lab-art.mjs` all checks pass; frontend/backend tilemap `cmp`
  byte-identical.
- Assets: `assets:verify` rc=0; `assets:verify:release` rc=1 (correct — assets
  blocked pending A0 audit).
- E2E world apply/revert + conflict + admin kill-switch drill: 8 passed.

## Blocked (real, documented — not faked)

- **V04–V06 (adapter real-machine segment)** — no real runtime endpoint
  configured (`LAB_HERMES/OPENCLAW/COMPUTER_USE_BASE_URL` empty). See
  `T1-P2-blocking-report.md` + `ADR-lab-runtime-adapter.md`. ADR stays 未选型.
- **V11 (OCI production isolation)** — no container runtime here, and this
  sandbox is not a qualifying dedicated Linux runner. Provisioning script +
  README in `deploy/lab-oci-runner/`. `lab_oci_enabled` stays False.
- **`$visual-verdict ≥90` (A2/A3/A4/A5, V18–V22 visual)** — requires a
  rendering + visual-QA environment (browser/Playwright or in-app render) not
  available in this session. Structural/logic parts are landed or specced; the
  pixel-art + screenshot-matrix + verdict loop is the deferred step.

## Remaining (feasible follow-up, in priority order)

### T7 follow-up — orchestrator wiring (small)

The role layer + all P4-exit invariants are DONE and tested (`workers.py`,
`test_lab_workers.py`, 9 green). The only follow-up is calling `delegate_worker`
inside `orchestrator.run_one_v1`'s real run lifecycle (integration wiring, not a
safety invariant).

### T8 follow-up — remaining alert wire points (small)

`telemetry.emit_alert` is wired at budget-exhaustion and world-apply-failure;
the other 5 alerts (orphan_heartbeat, stale_epoch, blocked_egress,
approval_timeout, cleanup_quarantine) are single best-effort calls at the sites
listed in `T8-hardening.md`. Chaos/capacity + staging drills are infra-blocked.

### T4 — A2 map polish & status FX (art; visual-QA blocked)

`lab_fx_32.png/json` beacon animations, `researching` icon, GameScene/
StatusVisuals wiring. The state-resolution rules (art-spec 6 rules) are code and
can be built; the FX **art** + `$visual-verdict` need a rendering env. The T3
building is a structurally-correct blockout awaiting seamless furniture art
(interiors_pt sandbox pods / server racks / Governor table).

### T6 — A3/A4 frontend UI migration (frontend; depends on T5, visual-QA blocked)

`ExperimentPanel.tsx` rebuild (4-track timeline, dual Task/Run state, exec vs
world-governance approval split, artifact list, apply/revert convergence, 3
viewports + reduced motion + 44px touch + contrast). Backend projections it
consumes are ready (T5). The build + `$visual-verdict ≥90` loop needs the
rendering env; Playwright introduction needs explicit approval (hard-#6).

### T8 — P5 hardening (mixed)

Content-free telemetry + alerts (orphan heartbeat, stale epoch, blocked egress,
approval timeout, budget exhaustion, apply/revert failure), retention/cleanup
drills, chaos/capacity tests, staging kill/rollback runbook, and a
production-isolation candidate (gVisor/Kata/Firecracker) evaluation record. The
telemetry taxonomy + isolation evaluation + runbook are doc/code deliverables
feasible without a rendering env; chaos/capacity + staging drills need infra.
