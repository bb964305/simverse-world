"""Conformance candidate wrapper for the Simverse reference runtime (Phase 7).

Exposes the adapter-gate's duck-typed hooks so ``adapter_gate.run_conformance``
can SCORE this runtime with real evidence. The tool intent and provider-event
stream it reports are DERIVED FROM A REAL ``RefAgent.run()`` (real LLM under
production config; a deterministic fake under test), so the gate scores the
runtime's actual behaviour. The runtime holds no infra handle and never bypasses
the Broker, so it is structurally admissible on the mandatory dimensions.
"""
from __future__ import annotations

import os

from app.lab.protocol import HandshakeManifest
from app.lab.runtime_ref.agent import AgentResult

LICENSE_MANIFEST = os.path.join(os.path.dirname(__file__), "ops-manifest.json")


class SimverseRefCandidate:
    name = "simverse_ref"
    bypass_broker = False            # every effect goes through the Broker
    accepts_infra_handles = False    # a plain HTTP/LLM process; no DB/Redis/world creds
    license_manifest_path = LICENSE_MANIFEST

    def __init__(self, result: AgentResult):
        self._result = result
        self._alive = True
        self._cancelled = False

    def handshake_manifest(self) -> HandshakeManifest:
        return HandshakeManifest(
            protocol_version=1, runtime=self.name, runtime_version="1.0",
            capabilities=["broker_mediation", "streaming", "cancel", "subagent"],
            cancel_behavior="cooperative",
        )

    def emit_tool_intent(self):
        """The first tool the real agent loop intended (falls back to a granted
        web.search if the loop concluded without a tool)."""
        if self._result.tool_intents:
            return self._result.tool_intents[0]
        return ("web.search", {"query": "conformance"})

    def provider_events(self):
        """The real step stream as (cursor, payload) — the provider cursor is the
        1-based step index, distinct from the ledger seq."""
        return [
            (i + 1, {"summary": s.summary, "phase": s.phase})
            for i, s in enumerate(self._result.steps)
        ] or [(1, {"summary": "noop"})]

    def subagent_child_caps(self, parent_caps):
        """A real depth-1 attenuation: intersect the parent's caps with the Scout
        role template (never an escalation)."""
        from app.lab.workers import ROLE_TEMPLATES
        scout = ROLE_TEMPLATES["scout"].capabilities
        return sorted(set(parent_caps) & scout)

    # cooperative cancel surface (the supervision probe drives these)
    async def cancel(self, handle):
        self._cancelled = True
        self._alive = False

    async def terminate(self, handle):
        self._alive = False

    async def kill(self, handle):
        self._alive = False

    async def health(self, handle):
        return {"alive": self._alive, "cancelled": self._cancelled}
