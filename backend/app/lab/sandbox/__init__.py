"""Sandbox adapter registry.

``get_adapter`` maps an adapter name to an instance, importing lazily so the
real (P2) adapters — which may pull heavy optional deps — never break ``import``
when unconfigured.

Resolution is **fail-closed** (recovery plan Phase 1, hard-constraint): only an
explicit ``mock`` selects Mock. A real runtime name constructs its adapter
(import-safe; it fail-closes at ``start()`` while unconfigured). An unknown
name, an empty/implicit name, or a real adapter whose import fails raises
``LabAdapterUnavailable`` — the registry must never silently fall back to Mock,
because a configured real runtime that quietly executes Mock work would be a
security/correctness lie.
"""
import logging

from app.lab.sandbox.base import (  # noqa: F401
    ArtifactSpec,
    LabAdapterUnavailable,
    RunSpec,
    SandboxAdapter,
    StepEvent,
)

logger = logging.getLogger(__name__)

_REAL_ADAPTERS = ("simverse_ref", "openclaw", "hermes", "computer_use")


def get_adapter(name: str) -> SandboxAdapter:
    key = (name or "").strip().lower()
    if key == "mock":
        from app.lab.sandbox.mock import MockAdapter
        return MockAdapter()
    if key in _REAL_ADAPTERS:
        try:
            if key == "simverse_ref":
                from app.lab.sandbox.simverse_ref import SimverseRefAdapter
                return SimverseRefAdapter()
            if key == "openclaw":
                from app.lab.sandbox.openclaw import OpenClawAdapter
                return OpenClawAdapter()
            if key == "hermes":
                from app.lab.sandbox.hermes import HermesAdapter
                return HermesAdapter()
            from app.lab.sandbox.computer_use import ComputerUseAdapter
            return ComputerUseAdapter()
        except LabAdapterUnavailable:
            raise
        except Exception as exc:  # ImportError / construction failure — fail closed.
            logger.error("lab adapter %r failed to load; refusing mock fallback", name, exc_info=True)
            raise LabAdapterUnavailable(
                f"lab adapter {name!r} failed to import/construct; refusing to fall back to mock"
            ) from exc
    raise LabAdapterUnavailable(
        f"unknown lab adapter {name!r}; only explicit 'mock' selects Mock (refusing fallback)"
    )
