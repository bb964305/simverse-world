# Historical Lab P7 v1 adapter evidence (superseded)

> **Invalid for Approved-v10 P3 or release approval.** This directory is retained
> unchanged as historical v1 evidence. Its 100/100 scores exercised a buffered
> loop that could finish before the Broker's real result returned; they do not
> prove same-turn result resume, commit-before-ACK, replay/backpressure, or
> Artifact provenance and must not enable a Runtime or satisfy AC07-AC09.

Current authority is `docs/adr/ADR-lab-runtime-adapter.md` plus the Approved-v10
blocker-resolution plan. The protocol-v2 P3/P4 proof and P4b world boundary are
deterministic and default-off; D0, production topology, staging, visual/assets,
and release push remain blocked.

## Archived v1 record

Under the superseded recovery-plan Phase 7 gate, a real, self-hosted, LLM-backed
agent runtime was built, run through the then-current conformance gate, and
scored 100/100. The files below are genuine records of that historical run, but
the score no longer represents selection under the current protocol or release
criteria.

## What was built

- `backend/app/lab/runtime_ref/agent.py` — a real agent loop that drives the
  project's configured **Anthropic-compatible LLM endpoint** (the same real
  endpoint the app already uses) to produce a bounded, protocol-shaped step
  sequence (think → tool_call → observation → message) + a terminal artifact. It
  only INTENDS tool calls; the Gateway's Broker mediates every effect.
- `backend/app/lab/runtime_ref/server.py` — a standalone HTTP server speaking the
  Lab wire protocol (`HttpAgentAdapter`): `POST /runs`, `/goal`, `GET /steps`,
  `/artifacts`, `/approve`, `/stop|cancel|terminate|kill`, `/health`.
- `backend/app/lab/runtime_ref/candidate.py` — the conformance candidate that
  exposes the gate's duck-typed hooks, derived from a REAL agent run.
- `backend/app/lab/sandbox/simverse_ref.py` — the `SimverseRefAdapter`, registered
  in `get_adapter` (fail-closed until an endpoint is configured).

## Measured result (`verdict.json`, reproducible)

Driven by the real LLM (`deepseek-v4-flash` via the Anthropic-compatible
endpoint), the agent produced a genuine multi-step research plan for
"cyberpunk aesthetics": **web.search → browser.navigate → code.run** (a real
BeautifulSoup scraping snippet), 10 steps, **2161 real model tokens**.

The conformance gate scored the candidate:

    broker_mediation          1.00  (ungranted denied, granted admitted → approved)
    disconnect_replay_cancel  1.00  (cursor dedup, replay window, fenced grants+epoch)
    isolated_deployment       1.00  (no DB/Redis/world handle)
    subagent_attenuation      1.00  (child caps ⊆ parent)
    ops_licensing             1.00  (manifest present)
    TOTAL = 100.0 / 100 → passed_mandatory=True, eliminated=False, SELECTED=True

Reproduce:
- Hermetic (fake completer): `pytest tests/test_lab_runtime_ref.py
  tests/test_lab_runtime_ref_server.py` → 6 passed.
- Real LLM: `LAB_REF_REAL_LLM=1 pytest
  tests/test_lab_runtime_ref.py::test_ref_agent_real_llm` → passed (~10s, real
  round-trip).
- Real verdict: `python scripts/p7_conformance.py` → writes this `verdict.json`.

## Honest boundary

- The reference runtime is **first-party / self-hosted**, NOT a commercial
  Hermes/OpenClaw/computer-use. Those third-party runtimes were NOT scored — no
  endpoints exist for them, and fabricating scores is refused. They stay
  import-safe + fail-closed; a future ADR scores them if endpoints are supplied.
- "Selected" means the runtime is an admissible real Adapter. It is NOT enabled by
  default (`lab_adapter=mock`). Production enablement needs: deploying the runtime
  server as an isolated service + setting `lab_simverse_ref_base_url`; routing its
  code/shell tool effects through the OCI executor on a dedicated Linux host (V11,
  `../lab-oci-evidence/`); and a staging canary.
- The server currently completes the loop then serves buffered steps via the same
  poll-with-cursor protocol; live incremental streaming is a follow-up.

## Real-socket deployment proof (2026-07-20)

The reference runtime was also run as a STANDALONE HTTP SERVICE over a real socket
(`python -m app.lab.runtime_ref.server` on 127.0.0.1:8900) and driven over real
HTTP + the real LLM (`real-socket-e2e.txt`): POST /runs → session, POST /goal →
the live model produced 8 real protocol steps (think→web.search→browser.navigate→
message, ~1443 real tokens) with the genuine conclusion "neon-lit cityscapes and
cybernetic body modifications", GET /artifacts → the real text artifact, done=true.
This closes the earlier "deploy the runtime server behind a real socket" follow-up
— the only remaining production step is OCI-isolated tool execution + a staging
canary (not a code deliverable).

## Second real candidate — commercial qwen3.7 endpoint (2026-07-20)

The user supplied a commercial LLM endpoint in `.env` (Alibaba DashScope Coding,
`coding.dashscope.aliyuncs.com/apps/anthropic`, model `qwen3.7-plus`, Anthropic-
messages-compatible). It was probed live (real round-trip, `PONG`) and then used
to drive a REAL agent loop as a second conformance candidate (`agent_qwen`):

- Real agent loop on the commercial qwen endpoint: 5 steps, 1 tool intent, **1403
  real model tokens**.
- Conformance verdict (`verdict-agent-qwen.json`): all five dimensions 1.00,
  **TOTAL=100.0, SELECTED=True**.

Honest framing: this scores the runtime BACKED BY a commercial model provider —
proving the runtime is portable across commercial endpoints — via `scripts/
p7_score_agent_endpoint.py`. It is NOT a distinct third-party agent-runtime
PRODUCT with its own wire protocol (those, e.g. a native computer-use API, would
need a translation shim). The API key was never printed or committed (`.env` is
gitignored; a pydantic `extra_forbidden` validation leak was caught and the temp
file deleted; `agent_*` are now declared Settings fields so no validation error
echoes the secret).

Historical result only: two model-backed v1 runs passed the former gate at
100/100. Neither score is a current protocol-v2 selection or release verdict.
