# ADR: Simverse Lab Runtime Adapter Selection

- **Status: Proposed / 未选型 (undecided)**
- **Gate scope:** This selection gate blocks **real-runtime enablement and any
  production-capability claim** — not Mock-backed control-plane correctness work.
  Mock/economy/governance/frontend hardening may proceed and is never counted as
  real-runtime evidence; enabling a real Adapter or OCI path still requires a
  recorded ≥80/100 selection with every mandatory dimension ≥0.6.
- Date: 2026-07-18
- Context tasks: P2-F (executable adapter gate + this ADR), building on P2-D
  (supervision) and P2-E (OCI executor).
- Decision owners: Lab platform (Jimmy + agents).

## Status summary (honest, load-bearing)

**No real runtime adapter has been selected.** The scoring/conformance *framework*
is complete and proven, but the actual head-to-head evaluation of the candidate
runtimes has **not** been run, because it requires **real runtime endpoints that
are not configured on any machine in this session**:

- `LAB_HERMES_BASE_URL` / `LAB_HERMES_API_KEY` — empty
- `LAB_OPENCLAW_BASE_URL` / `LAB_OPENCLAW_API_KEY` — empty
- `LAB_COMPUTER_USE_BASE_URL` / `LAB_COMPUTER_USE_API_KEY` — empty
- (a "Grok"-class candidate, per PRD framing) — no endpoint configured

**Hard blocker = missing real runtime endpoints (`LAB_HERMES_BASE_URL` et al.
unset).** Fabricating scores against a runtime we cannot actually exercise would
be dishonest and is explicitly refused.

**Current decision: the system keeps the Mock adapter as the ONLY enabled
runtime.** No real adapter is wired into the default or any enabled path
(`settings.lab_adapter = "mock"`, `lab_oci_enabled = False`). Per the PRD, if
neither candidate can be shown to pass, the system stays on Mock, P2 selection is
halted, and a new/updated ADR is required to move forward. This ADR fixes that
undecided state in writing rather than papering over it.

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
