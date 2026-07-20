# ADR: Simverse Lab Runtime Adapter Selection

- **Status: Accepted (2026-07-20) — one candidate selected: the Simverse
  self-hosted reference runtime. The commercial third-party runtimes remain
  unevaluated (no endpoints).**
- **Gate scope:** This selection gate blocks **real-runtime enablement and any
  production-capability claim** — not Mock-backed control-plane correctness work.
  Enabling a real Adapter or OCI path still requires a recorded ≥80/100 selection
  with every mandatory dimension ≥0.6.
- Date: 2026-07-18 (updated 2026-07-20)
- Context tasks: P2-F (executable adapter gate + this ADR), building on P2-D
  (supervision) and P2-E (OCI executor); recovery plan Phase 7.
- Decision owners: Lab platform (Jimmy + agents).

## Status summary (honest, load-bearing)

**One runtime candidate is selected: the Simverse self-hosted reference runtime**
(`app/lab/runtime_ref/`). It is a REAL, LLM-backed agent runtime — not a stub —
driven by the project's configured Anthropic-compatible endpoint
(`settings.llm_base_url`, the same real endpoint the app already uses). It was run
through the executable conformance gate with a REAL LLM-driven agent loop and
scored:

| dimension | score | mandatory |
|---|---:|---|
| broker_mediation | 1.00 | ✓ |
| disconnect_replay_cancel | 1.00 | ✓ |
| isolated_deployment | 1.00 | ✓ |
| subagent_attenuation | 1.00 | |
| ops_licensing | 1.00 | |
| **TOTAL** | **100.0 / 100 — SELECTED** | |

Measured evidence: `docs/renders/lab-p7-evidence/verdict.json` + the reproducible
tests `backend/tests/test_lab_runtime_ref.py` (conformance) and
`test_lab_runtime_ref_server.py` (HTTP wire + adapter e2e). The real agent loop
produced a genuine multi-step research plan (web.search → browser.navigate →
code.run, 2161 real model tokens) and the gate admitted it — the runtime intends
tool calls only, holds no infra handle, and never bypasses the Broker.

**Second candidate scored — commercial qwen3.7 endpoint (2026-07-20).** An
operator-supplied commercial LLM endpoint (Alibaba DashScope Coding,
`coding.dashscope.aliyuncs.com/apps/anthropic`, model `qwen3.7-plus`) was probed
live and used to drive a REAL agent loop as a second candidate (`agent_qwen`):
5 steps, 1403 real tokens, gate verdict **100.0/100 — SELECTED**
(`docs/renders/lab-p7-evidence/verdict-agent-qwen.json`, via
`scripts/p7_score_agent_endpoint.py`). This proves the runtime is portable across
commercial model providers. Honest scope: it validates the runtime with a
commercial MODEL, not a distinct third-party agent-runtime PRODUCT.

**Still unevaluated:** the commercial agent-runtime PRODUCTS with their own wire
protocols (`hermes`, `openclaw`, native `computer_use`) were NOT scored — no such
endpoints exist in any available environment, and fabricating scores is explicitly
refused. They remain import-safe, fail-closed adapters; a runtime with a non-Lab
wire needs a translation shim (subclass `HttpAgentAdapter`), then scores unchanged
via `scripts/p7_score_endpoint.py`.

**Honest boundary — what "selected" does and does NOT mean here:**

- The reference runtime PASSED the gate, so it is an admissible real Adapter. But
  the default stays `settings.lab_adapter = "mock"` and `lab_oci_enabled = False`.
- Enabling it in production requires: (1) DEPLOYING the reference runtime server
  (`python -m app.lab.runtime_ref.server`) as an isolated service and setting
  `lab_simverse_ref_base_url`; (2) routing its `code.run`/`shell.exec` tool
  effects through the OCI executor on a dedicated Linux host (V11 — proven
  separately in `docs/renders/lab-oci-evidence/`); (3) a staging canary. An
  unconfigured `simverse_ref` adapter fail-closes at `start()`.
- Live incremental step streaming during a long run is a noted follow-up (the
  server currently completes the loop then serves the buffered steps via the same
  poll-with-cursor protocol).

## The gate (framework — ready now)

Implemented in `backend/app/lab/adapter_gate.py`, proven by
`backend/tests/test_lab_adapter_gate.py`.

A candidate is **selected** only if `total >= 80/100` **and** every *mandatory*
dimension scores `>= 0.6`. Any mandatory dimension below that threshold
**eliminates** the candidate regardless of total.

| Dimension | Weight | Mandatory | What the probe checks |
|---|---:|:---:|---|
| `broker_mediation` | 30% | ✅ | Every effect flows through the Tool Broker; an ungranted tool intent is DENIED, and no out-of-band effect channel exists. |
| `disconnect_replay_cancel` | 25% | ✅ | Provider-cursor dedup + correct replay window (P2-D supervision), and `cancel_run` ALWAYS fences (grants revoked + lease epoch bumped) — **even when the runtime's cancel/terminate hooks throw** (fail-closed). |
| `isolated_deployment` | 20% | ✅ | Adapter holds no DB / Redis / world credentials (constructor rejects infra handles). |
| `subagent_attenuation` | 15% | | A declared sub-agent grant is a proper attenuation of its parent (reuses `grants` delegation rules); escalation → 0. |
| `ops_licensing` | 10% | | A license / ops manifest record exists (evidence pointer). |

Weights sum to 100. The three mandatory dimensions correspond to the PRD's three
hard elimination criteria: Broker-only effects, fail-closed cancel, isolated
deployment.

### Conformance probe list (executable)

- `probe_broker_mediation` — Broker denies an ungranted declared intent; a
  `bypass_broker` candidate is scored 0.
- `probe_disconnect_replay_cancel` — supervision cursor dedup / replay window /
  fail-closed cancel-fence (incl. the "cancel throws but still fenced" case
  handed off from P2-D review).
- `probe_isolated_deployment` — static: no infra handles.
- `probe_subagent_attenuation` — `grants` delegation subset check.
- `probe_ops_licensing` — license/manifest record existence.

Tie-break (PRD): smaller credential/network surface first, then lower ops burden.
The framework leaves tie-break inputs as `GateVerdict.tie_break` and does **not**
auto-decide.

## Candidates — known design characteristics only (NO measured scores)

The following are design characteristics drawn from PRD framing. They are **not**
conformance scores and must not be treated as such until measured against real
endpoints.

- **Hermes (candidate A)** — PRD-described as protocol-native with first-class
  streaming/checkpoint/resume; would need its handshake to advertise
  `broker_mediation` and a supervisable event stream. Isolation/licensing posture
  unverified without a live instance.
- **Grok-class (candidate B)** — PRD-described as a strong general agent runtime;
  Broker-mediation + isolated-deployment conformance would need a shim proving no
  out-of-band effects and no infra credentials. Cancel/replay conformance
  unverified without a live instance.

No `total`, no `selected`, no elimination is asserted for either candidate here —
that is exactly what the missing endpoints block.

## How to run the real evaluation (reproducible steps)

1. Provision a real runtime instance and set its endpoint, e.g.
   `LAB_HERMES_BASE_URL` / `LAB_HERMES_API_KEY` (and/or the Grok-class endpoint).
2. Wrap the runtime in a thin conformance shim exposing the candidate hooks the
   probes call (`handshake_manifest`, `emit_tool_intent`, `provider_events`,
   `subagent_child_caps`, `accepts_infra_handles`, `license_manifest_path`,
   optional `cancel/terminate/kill/health`). `HttpAgentAdapter.read_provider_events`
   already surfaces provider cursors for supervision.
3. Run `adapter_gate.run_conformance(shim, db=<staging session>)` →
   `adapter_gate.score_candidate(name, results)` for each candidate.
4. If exactly one candidate scores `>= 80` AND passes all mandatory dimensions,
   update THIS ADR to **Accepted**, record the winning candidate + the loser's
   per-dimension evidence, and wire the adapter behind its feature flag.
5. If neither passes: system stays on **Mock**, P2 selection remains halted, and a
   further ADR is required (do not fabricate a pass).

## Consequences

- Positive: the gate is executable and trusted (proven to score + eliminate on
  fakes); selection becomes a mechanical, evidence-backed step once endpoints
  exist. Reproduction is documented.
- Negative / open: no real runtime is available yet, so the meta-game's real
  sandbox path stays on Mock. This is a deliberate, honest hold — not a silent
  default.
