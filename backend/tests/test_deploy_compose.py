"""P0-3b invariants for the deploy compose (supersedes the P0-3a gating).

P0-3a kept the agent loops inside the API process and gated the standalone
agent-worker behind an opt-in profile, because WS broadcasts went through the
in-process ConnectionManager and a worker-side broadcast would be a dead letter.

P0-3b landed the cross-process bus (Redis pub/sub + Redis-backed locks/queues/
presence/counters), so the deploy topology now is:
- a `redis` service (the shared bus),
- a one-shot `bootstrap` service that migrates and synchronises built-in data,
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


def test_sprite_provider_secret_is_worker_only_and_artifacts_are_shared():
    services = _load_services()
    api_env = _env_as_dict(services["api"])
    worker_env = _env_as_dict(services["agent-worker"])
    api_env_files = services["api"].get("env_file") or []
    worker_env_files = services["agent-worker"].get("env_file") or []
    if isinstance(api_env_files, str):
        api_env_files = [api_env_files]
    if isinstance(worker_env_files, str):
        worker_env_files = [worker_env_files]
    worker_paths = {
        item.get("path") if isinstance(item, dict) else item
        for item in worker_env_files
    }
    api_paths = {
        item.get("path") if isinstance(item, dict) else item
        for item in api_env_files
    }
    assert "RESIDENT_SPRITE_PROVIDER_API_KEY" not in api_env
    assert "RESIDENT_SPRITE_PROVIDER_API_KEY" not in worker_env
    assert "resident-sprite-worker.env" in worker_paths
    assert "resident-sprite-worker.env" not in api_paths
    assert worker_env.get("RESIDENT_SPRITE_CAPABILITY_RECEIPT")
    for name in ("api", "agent-worker"):
        volumes = services[name].get("volumes") or []
        rendered = [item for item in volumes if isinstance(item, str)]
        assert "resident_sprite_artifacts:/var/lib/simverse/resident-sprites" in rendered


def test_resident_sprite_generation_is_disabled_by_default():
    services = _load_services()
    for name in ("api", "agent-worker"):
        value = _env_as_dict(services[name]).get("RESIDENT_SPRITE_ENABLED", "")
        assert value == "${RESIDENT_SPRITE_ENABLED:-false}"


def test_api_and_worker_depend_on_redis():
    """Both processes must wait for Redis before starting."""
    services = _load_services()
    for name in ("api", "agent-worker"):
        assert "redis" in _depends_on(services[name]), (
            f"{name} must depend_on redis so it doesn't start before the bus"
        )


def test_builtin_roster_bootstrap_precedes_api_and_worker():
    """A deploy must replace legacy built-ins before any world loop starts."""
    services = _load_services()
    bootstrap = services.get("bootstrap")
    assert bootstrap is not None, "missing one-shot production bootstrap"
    assert bootstrap.get("restart") == "no"
    command = str(bootstrap.get("command") or "")
    assert "alembic upgrade head" in command
    assert "python -m seed.reset_builtin_residents" in command

    for name in ("api", "agent-worker"):
        dependency = (services[name].get("depends_on") or {}).get("bootstrap")
        assert dependency == {"condition": "service_completed_successfully"}, (
            f"{name} may start before the built-in roster is synchronised"
        )
