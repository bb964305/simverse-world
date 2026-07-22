# ADR: Simverse Lab Runtime Adapter Selection

- **Status: Accepted for the default-off P3/P4 protocol-v2 local boundary.
  Production enablement and release selection remain blocked.**
- **Gate scope:** This selection gate blocks **real-runtime enablement and any
  production-capability claim** — not default-off local protocol correctness work.
  Local evidence does not satisfy D0, P5, staging, visual, asset, or release
  gates.
- Date: 2026-07-18 (updated 2026-07-22)
- Context tasks: Approved-v10 P2 protocol/session boundary, P3 real
  result/supervision loop, and P4 durable control, retaining the earlier
  recovery-plan selection record only as historical evidence.
- Decision owners: Lab platform (Jimmy + agents).

## Approved-v10 protocol-v2 decision (2026-07-21)

This section supersedes the v1 scoring and completion language retained below as
historical evidence. The earlier 100/100 runs exercised the buffered v1 protocol,
which could complete the model loop before Broker execution and therefore did
not prove real result resume, ACK/replay, or Artifact provenance. Those scores
must not be used to enable a real Runtime or satisfy the P3/release gate.

The Simverse reference Runtime remains the only admitted local protocol-v2
implementation. P2 established its session boundary:

- handshake is strict v2, `broker_only`, `session_affine`, and requires
  deterministic create/reattach capability;
- Gateway registration commits before provider create and is bound to the exact
  live lease owner/epoch; same-host restart reattaches, while host/volume loss or
  a divergent provider locator quarantines;
- Runtime state and command receipts use a durable hardened SQLite file;
- all run routes authenticate a short-lived, action-scoped `lab-runtime` JWT
  before session lookup, with current/next key rotation and exact retry binding;
- the P2 checkpoint exposed no v2 execution handler, so admission failed closed
  before side effects until the P3 loop was present.

P3 now supplies the default-off, deterministic result loop without using the
historical buffered `step_stream` path:

- the Runtime durably pauses the same model turn at `tool_intent`; only an exact,
  bounded `succeeded|denied|failed` Broker result can resume it;
- the Gateway commits canonical event/turn/intent state before provider ACK,
  accepts exact replay idempotently, rejects divergent bindings or cursor
  regression, and enforces the 128-event plus byte backpressure window;
- Broker result and command identity are durable before delivery. A lost Gateway
  receipt is recovered by resending the same command and CAS-recording the
  Runtime's idempotent receipt;
- finalization requires zero pending intents and every result to be
  `runtime_acked`. Success additionally requires a real succeeded result, and
  Artifact metadata is rebuilt from Gateway-owned Broker result rows; and
- the v2 HTTP adapter uses authenticated handshake, goal, event poll/ACK, result,
  and Artifact endpoints. The v2 orchestrator never consumes `step_stream`.

The canonical local evidence is release step
`run-all:ac07-runtime-result-loop`. Its deterministic unit set covers the frozen
protocol, legacy compatibility, Runtime HTTP/store/restart, result recovery,
Gateway supervision, replay/backpressure, denial/failure, and the full sentinel
round trip; a separate required real-Postgres suite covers commit-before-ACK,
durable backpressure, and result-receipt/finalization recovery. The opt-in live
LLM check is deliberately not part of this P3 oracle: model randomness cannot
replace the deterministic Broker-result proof.

P4 completes the default-off control boundary. Run cancel and global kill are
durable requests owned by the Runner, not provider calls from the API. Runtime
control uses action-scoped JWTs bound to run, session, epoch, and action; exact
receipts survive restart, while stale or divergent commands fail closed.
Runtime and Executor targets are fenced independently, missing inventory is
quarantined, and v2 queue claims are reclaimed without duplicate execution.
Lifecycle startup refuses global admission unless both D0-provisioned Runtime
and Executor controllers are present.

The standalone development entrypoint is now explicit. Running
`python -m app.lab.runtime_ref.server` requires
`LAB_RUNTIME_PROTOCOL_VERSION`. Version 2 additionally requires
`LAB_RUNTIME_STORE_PATH`, `LAB_RUNTIME_AUTH_ISSUER`, exact audience
`lab-runtime`, and a JSON current/next keyring. Missing or partial configuration
exits; importing `module:app` exposes no `/runs` route. Version 1 is available
only when explicitly requested for legacy compatibility.

All rollout flags remain false. No Runtime service, image, production network,
TLS/mTLS identity, or production durable volume is authorized while D0 is
absent. P4b world fencing is verified separately for the default-off source
checkpoint, but P5 production topology, staging, and release push remain
blocked. This ADR records a local P3/P4 implementation selection only, not
production or release approval.

## Historical v1 selection evidence (superseded for protocol v2)

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
- Under the former v1 assumptions, production enablement would have required
  deploying the server and setting `lab_simverse_ref_base_url`. Approved-v10 now
  additionally requires D0, the explicit v2 standalone configuration above, P3
  result-loop evidence, isolated Executor routing, and a staging canary. An
  unconfigured `simverse_ref` adapter still fails closed at `start()`.
- The buffered v1 loop described below is not an accepted v2 execution path. P3
  must pause at intent and resume the same turn only from the real Broker result.

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
- Negative / open: the P3/P4-qualified Runtime remains default-off while D0/P5,
  staging, visual, asset, and release gates are open. This is a deliberate
  fail-closed hold rather than a production selection.
