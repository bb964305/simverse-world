"""P0-3b invariants for the deploy compose (supersedes the P0-3a gating).

P0-3a kept the agent loops inside the API process and gated the standalone
agent-worker behind an opt-in profile, because WS broadcasts went through the
in-process ConnectionManager and a worker-side broadcast would be a dead letter.

P0-3b landed the cross-process bus (Redis pub/sub + Redis-backed locks/queues/
presence/counters), so the deploy topology now is:
- a `redis` service (the shared bus),
- the `api` service delegating background loops (RUN_BACKGROUND_TASKS=false) and
  free to run multiple uvicorn workers,
- the `agent-worker` service starting by default (no profile gate),
- both api and agent-worker pointed at Redis via REDIS_URL.
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


def _depends_on(service: dict) -> set[str]:
    dep = service.get("depends_on") or {}
    if isinstance(dep, list):
        return set(dep)
    return set(dep.keys())


def test_redis_service_present_with_healthcheck():
    """The shared cross-process bus must exist and be health-gated."""
    services = _load_services()
    redis = services.get("redis")
    assert redis is not None, "no redis service — the P0-3b cross-process bus is missing"
    assert redis.get("healthcheck"), "redis service must define a healthcheck"


def test_api_delegates_background_tasks_to_worker():
    """With the Redis bus in place, the API must NOT run the loops in-process
    (that would duplicate them across workers)."""
    env = _env_as_dict(_load_services()["api"])
    value = env.get("RUN_BACKGROUND_TASKS", "").strip().lower()
    assert value in ("false", "0", "no", "off"), (
        "api should set RUN_BACKGROUND_TASKS=false post-P0-3b so the standalone "
        "agent-worker owns the loops and the API can scale to multiple workers"
    )


def test_agent_worker_starts_by_default():
    """The agent-worker must start on a plain `docker compose up` now that its
    broadcasts reach players over Redis pub/sub."""
    worker = _load_services().get("agent-worker")
    assert worker is not None, "agent-worker service is missing"
    profiles = worker.get("profiles") or []
    assert not profiles, (
        "agent-worker is still profile-gated; P0-3b's pub/sub bus means its WS "
        "broadcasts are no longer dead letters, so it should start by default"
    )


def test_api_and_worker_point_at_redis():
    """Both the API and the agent-worker must be configured with REDIS_URL."""
    services = _load_services()
    for name in ("api", "agent-worker"):
        env = _env_as_dict(services[name])
        assert env.get("REDIS_URL"), f"{name} is missing REDIS_URL"


def test_api_and_worker_depend_on_redis():
    """Both processes must wait for Redis before starting."""
    services = _load_services()
    for name in ("api", "agent-worker"):
        assert "redis" in _depends_on(services[name]), (
            f"{name} must depend_on redis so it doesn't start before the bus"
        )
