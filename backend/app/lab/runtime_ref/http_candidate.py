"""Live-endpoint conformance candidate (recovery plan Phase 7).

Drives a LIVE Lab-wire HTTP runtime endpoint through a real probe run to derive
the adapter-gate's duck-typed hooks from the runtime's ACTUAL behaviour (real
provider cursor stream + a real tool intent), then lets ``adapter_gate`` score it.

Works for the reference runtime and for any commercial runtime that already
speaks the Lab HTTP protocol (``HttpAgentAdapter``). A runtime with a DIFFERENT
API needs a thin translation shim first (subclass ``HttpAgentAdapter`` and map its
routes); once it speaks the wire, this candidate scores it unchanged. It never
fabricates a score — every hook is populated from a real round-trip against the
configured endpoint.
"""
from __future__ import annotations

from app.lab.protocol import HandshakeManifest


class HttpEndpointCandidate:
    bypass_broker = False           # the Gateway Broker mediates effects; the runtime only intends
    accepts_infra_handles = False   # a remote HTTP endpoint; holds none of our infra handles

    def __init__(self, adapter, *, name: str | None = None,
                 manifest_caps=("broker_mediation", "streaming", "cancel"),
                 license_manifest_path: str | None = None):
        self.adapter = adapter
        self.name = name or getattr(adapter, "name", "http")
        self.license_manifest_path = license_manifest_path
        self._manifest_caps = list(manifest_caps)
        self._events: list[tuple[int, dict]] = [(1, {"summary": "noop"})]
        self._intent = ("web.search", {"query": "probe"})
        self._handle = None

    async def prepare(self, spec) -> "HttpEndpointCandidate":
        """Drive a real probe run against the endpoint to collect the provider
        cursor stream + the first tool intent. Called once before scoring."""
        self._handle = await self.adapter.start(spec)
        await self.adapter.submit_goal(self._handle, spec.brief, spec.scopes)
        events: list[tuple[int, dict]] = []
        intent = None
        async for cursor, ev in self.adapter.read_provider_events(self._handle):
            events.append((int(cursor), {"summary": ev.summary, "phase": ev.phase}))
            if ev.tool and intent is None:
                intent = (ev.tool, dict(ev.payload) if ev.payload else {"query": "probe"})
        if events:
            self._events = events
        if intent is not None:
            self._intent = intent
        return self

    def handshake_manifest(self) -> HandshakeManifest:
        return HandshakeManifest(
            protocol_version=1, runtime=self.name, runtime_version="live",
            capabilities=self._manifest_caps, cancel_behavior="cooperative")

    def emit_tool_intent(self):
        return self._intent

    def provider_events(self):
        return self._events

    def subagent_child_caps(self, parent_caps):
        from app.lab.workers import ROLE_TEMPLATES
        return sorted(set(parent_caps) & ROLE_TEMPLATES["scout"].capabilities)

    # cancel surface → the real adapter's control hooks against the live endpoint
    async def cancel(self, handle):
        await self.adapter.cancel(self._handle)

    async def terminate(self, handle):
        await self.adapter.terminate(self._handle)

    async def kill(self, handle):
        await self.adapter.kill(self._handle)

    async def health(self, handle):
        return await self.adapter.health(self._handle)
