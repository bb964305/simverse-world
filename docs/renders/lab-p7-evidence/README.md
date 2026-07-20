# Lab P7 — real runtime adapter selected (Simverse reference runtime)

Recovery plan Phase 7. A REAL, self-hosted, LLM-backed agent runtime was built,
run through the executable conformance gate with a real LLM-driven agent loop,
and **SELECTED** (100/100, every mandatory dimension satisfied). This is genuine
real-runtime evidence — not Mock, not fabricated.

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
