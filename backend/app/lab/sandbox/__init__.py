"""Sandbox adapter registry.

``get_adapter`` maps an adapter name to an instance, importing lazily so the
real (P2) adapters — which may pull heavy optional deps — never break ``import``
when unconfigured. Unknown/failed adapters fall back to the Mock so a run never
hard-crashes the runner.
"""
import logging

from app.lab.sandbox.base import ArtifactSpec, RunSpec, SandboxAdapter, StepEvent  # noqa: F401

logger = logging.getLogger(__name__)


def get_adapter(name: str) -> SandboxAdapter:
    name = (name or "mock").lower()
    if name == "mock":
        from app.lab.sandbox.mock import MockAdapter
        return MockAdapter()
    try:
        if name == "openclaw":
            from app.lab.sandbox.openclaw import OpenClawAdapter
            return OpenClawAdapter()
        if name == "hermes":
            from app.lab.sandbox.hermes import HermesAdapter
            return HermesAdapter()
        if name == "computer_use":
            from app.lab.sandbox.computer_use import ComputerUseAdapter
            return ComputerUseAdapter()
    except Exception:  # ImportError or unconfigured runtime — degrade to mock.
        logger.warning("lab adapter %r unavailable; falling back to mock", name, exc_info=True)
    from app.lab.sandbox.mock import MockAdapter
    return MockAdapter()
