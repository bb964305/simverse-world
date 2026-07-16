"""Lab (experiment building) subsystem — the C-layer real-sandbox execution.

Kept decoupled from resident tick: B-layer (routers/services) only manages money
+ state machine and drops a run onto the Redis queue; the standalone Lab Runner
(``python -m app.lab.main``) consumes it, executes a SandboxAdapter, streams
steps back, and lands artifacts. See docs/FEATURE_SPEC_LAB.md §5.
"""
from app.redis_client import get_redis

# Runtime kill switch (spec §5.3): the Redis flag the admin toggles live —
# distinct from ``settings.lab_enabled`` (deploy-level, loaded at startup).
LAB_ENABLED_KEY = "sv:lab:enabled"


async def is_lab_runtime_enabled() -> bool:
    """True unless the admin has explicitly killed the Lab at runtime.

    Absent key = enabled (the deploy-level ``settings.lab_enabled`` already
    gates whether the feature is wired at all)."""
    val = await get_redis().get(LAB_ENABLED_KEY)
    if val is None:
        return True
    return str(val).lower() not in ("0", "false", "off", "no")


async def set_lab_runtime_enabled(enabled: bool) -> None:
    await get_redis().set(LAB_ENABLED_KEY, "1" if enabled else "0")
