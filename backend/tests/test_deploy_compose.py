"""P0-3a review fix: deploy compose must not activate split-worker mode yet.

WS broadcasts go through the in-process ConnectionManager (app/ws/manager.py),
so until P0-3b lands a cross-process bus (Redis pub/sub), running agent loops
in a separate container makes every worker-side broadcast a dead letter:
players would stop seeing resident_move / autonomous chat / heat updates.

Therefore the deploy compose must:
- keep the api service on the default RUN_BACKGROUND_TASKS (true, in-process)
- not start the agent-worker service by default (opt-in profile only)
"""
from pathlib import Path

import yaml

COMPOSE_PATH = (
    Path(__file__).resolve().parents[2] / "deploy" / "backend" / "docker-compose.yml"
)


def _load_services() -> dict:
    with COMPOSE_PATH.open() as f:
        compose = yaml.safe_load(f)
    return compose["services"]


def _env_as_dict(service: dict) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):  # ["K=V", ...] form
        env = dict(item.split("=", 1) for item in env)
    return {str(k): str(v) for k, v in env.items()}


def test_api_keeps_background_tasks_in_process():
    """API must not disable in-process loops before a cross-process WS bus exists."""
    env = _env_as_dict(_load_services()["api"])
    value = env.get("RUN_BACKGROUND_TASKS", "true").strip().lower()
    assert value not in ("false", "0", "no", "off"), (
        "api sets RUN_BACKGROUND_TASKS=false but WS broadcasts are in-process only; "
        "flipping this before P0-3b (Redis pub/sub) silences all realtime agent events"
    )


def test_agent_worker_is_opt_in_only():
    """agent-worker must not start on plain `docker compose up` until P0-3b."""
    services = _load_services()
    worker = services.get("agent-worker")
    if worker is None:
        return  # not defined at all — trivially not started
    profiles = worker.get("profiles") or []
    assert profiles, (
        "agent-worker has no compose profile, so it starts by default; "
        "its WS broadcasts are dead letters until P0-3b lands a cross-process bus"
    )
