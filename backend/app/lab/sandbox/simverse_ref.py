"""Adapter for the Simverse reference runtime (recovery plan Phase 7).

The reference runtime is the one candidate that PASSED the adapter conformance
gate — see ``archive/2026-07-25/docs/adr/ADR-lab-runtime-adapter.md`` and
``archive/2026-07-25/docs/renders/lab-p7-evidence/``.
It speaks the same HTTP wire protocol as the other real adapters, so this is a
thin subclass of ``HttpAgentAdapter`` reading its own endpoint/credentials. Empty
``base_url`` = unconfigured → fail-closed at ``start()`` (the runtime must be
deployed and its URL set before a run can use it)."""
from app.config import settings
from app.lab.sandbox.base import HttpAgentAdapter


class SimverseRefAdapter(HttpAgentAdapter):
    name = "simverse_ref"

    def __init__(self) -> None:
        super().__init__(
            base_url=getattr(settings, "lab_simverse_ref_base_url", "") or "",
            api_key=getattr(settings, "lab_simverse_ref_api_key", "") or "",
        )
