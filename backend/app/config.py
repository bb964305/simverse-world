from pydantic import model_validator
from pydantic_settings import BaseSettings

_DEFAULT_JWT_SECRET = "dev-secret-change-in-production"


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/skills_world"
    # Dev convenience only: create tables from models at startup.
    # Production must run `alembic upgrade head` instead (P0-6).
    auto_create_tables: bool = False
    # Run background loops (agent, heat cron, embedding backfill) inside the
    # API process. True keeps single-process dev behavior; set False in
    # deployments where the standalone agent-worker owns the loops (P0-3).
    run_background_tasks: bool = True
    redis_url: str = "redis://localhost:6379/0"
    # Local development only; anything non-debug must set a real JWT_SECRET (P0-4b)
    debug: bool = False
    jwt_secret: str = _DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"

    @model_validator(mode="after")
    def _reject_default_jwt_secret(self) -> "Settings":
        if not self.debug and self.jwt_secret == _DEFAULT_JWT_SECRET:
            raise ValueError(
                "JWT_SECRET is still the insecure default — refusing to start. "
                "Set JWT_SECRET to a long random string, or set DEBUG=true for "
                "local development."
            )
        return self
    jwt_expire_minutes: int = 1440
    github_client_id: str = ""
    github_client_secret: str = ""
    github_redirect_uri: str = ""
    anthropic_api_key: str = ""
    # Custom LLM endpoint (overrides anthropic_api_key if set)
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_default_model: str = "claude-haiku-4-5-20251001"
    llm_max_tokens: int = 512
    llm_thinking: bool = False  # disable thinking/reasoning for faster responses
    # Per-attempt LLM usage telemetry (P1-1, llm_usage table). Metering writes
    # go to a separate short-lived session and never affect the LLM call; this
    # flag only gates whether the telemetry rows are persisted at all.
    llm_metering_enabled: bool = True

    # --- Observability (Phase 3) ---
    # Sentry stays inert unless a DSN is provided (sentry_sdk imported lazily).
    sentry_dsn: str = ""
    sentry_environment: str = "dev"
    sentry_traces_sample_rate: float = 0.0
    # Exposes GET /metrics (Prometheus). Per-process registry; see
    # app/observability.py for the multi-worker caveat.
    metrics_enabled: bool = True
    # Bearer token guarding GET /metrics (empty = open; set in deployments
    # where /metrics is publicly reachable, e.g. behind the CF tunnel).
    metrics_token: str = ""

    # --- Budget circuit breaker (P1-1, E-24/E-18) ---
    # Global daily spend cap (USD). Background LLM work degrades in three tiers
    # as this fills: >=80% throttle (tick x2), >=95% rule-only (force plan, no
    # inter-resident chat), >=100% background paused (only player-visible calls).
    # Default ≈ 15-resident baseline × 1.5; raise for larger worlds. Set 0 to disable.
    budget_global_daily_usd: float = 1.5
    # Per-user daily player-visible spend cap (USD); over it, player chat replies
    # with a friendly "daily limit reached" instead of calling the LLM. 0 disables.
    budget_user_daily_usd: float = 0.5
    # Per-request ceiling for a single forge generation (deep ≈ $0.15). 0 disables.
    budget_forge_request_usd: float = 0.15
    # Routing (E-18): background/system calls are pinned to this model (locked to
    # the cheap default); player-visible calls use the configurable effective_model.
    background_llm_model: str = ""

    @property
    def effective_api_key(self) -> str:
        return self.llm_api_key or self.anthropic_api_key

    @property
    def effective_model(self) -> str:
        return self.llm_model or self.llm_default_model

    @property
    def background_model(self) -> str:
        """Model for background/system LLM calls (E-18 routing control surface).

        Defaults to ``effective_model`` — i.e. no behavior change and no risk of
        sending an unknown model id to a relay endpoint (the deployment runs
        qwen3.7-plus via 百炼, where 'claude-haiku-*' is not a valid model, F-02).
        Ops pin background to a cheaper model by setting ``background_llm_model``;
        the player-visible path stays on ``effective_model`` regardless, so an
        upgrade there can't drag up the 88%-of-tokens background traffic."""
        return self.background_llm_model or self.effective_model
    cors_origins: list[str] = ["http://localhost:5173"]

    # --- LinuxDo OAuth (Plan 1) ---
    linuxdo_client_id: str = ""
    linuxdo_client_secret: str = ""
    linuxdo_redirect_uri: str = ""
    linuxdo_min_trust_level: int = 0

    # --- Portrait LLM (Plan 1) ---
    portrait_llm_model: str = "gemini-3-pro-image-preview"
    portrait_llm_base_url: str = ""
    portrait_llm_api_key: str = ""
    portrait_llm_timeout: int = 180

    # --- TTS (E5) ---
    tts_base_url: str = ""
    tts_api_key: str = ""
    tts_model: str = "tts-1"
    tts_daily_free_quota: int = 30

    # --- System LLM advanced params (Plan 1) ---
    system_llm_temperature: float = 0.3
    system_llm_timeout: int = 30
    system_llm_max_retries: int = 2

    # --- User LLM advanced params (Plan 1) ---
    user_llm_temperature_chat: float = 0.7
    user_llm_temperature_forge: float = 0.5
    user_llm_timeout: int = 120
    user_llm_max_retries: int = 3
    user_llm_concurrency: int = 5

    # --- Media Upload (P2) ---
    media_upload_dir: str = "backend/static/uploads"
    media_max_image_size: int = 5 * 1024 * 1024   # 5 MB
    media_max_video_size: int = 50 * 1024 * 1024  # 50 MB
    video_llm_model: str = "kimi-k2.5"

    # --- SearXNG (research) ---
    searxng_url: str = "http://localhost:58080"

    allow_user_custom_llm: bool = False

    # --- Embedding provider (PLAN_P3 后续批次 A) ---
    # Master switch: false = no embedding calls at all (memory columns stay
    # NULL, retrieval falls back to importance/recency). Set false on deploys
    # with no embedding endpoint instead of letting calls fail-and-log.
    embedding_enabled: bool = True
    # OpenAI-compatible endpoint (百炼 compatible-mode / one-api / OpenAI).
    # When set it takes priority over local Ollama below; `dimensions` is
    # passed explicitly (fixes the qwen3-embedding 2560→1024 truncation).
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 1024  # must match vector(1024) column

    # --- Ollama (local embedding fallback) ---
    ollama_base_url: str = "http://localhost:11434"
    ollama_embed_model: str = "qwen3-embedding:4b"
    ollama_embed_dimensions: int = 1024

    # --- Agent Loop ---
    agent_tick_interval: int = 60          # seconds between tick rounds
    agent_max_concurrent: int = 5          # max residents ticking in parallel
    agent_max_daily_actions: int = 20      # per-resident action cap per in-game day
    agent_chat_max_turns: int = 8          # max dialog turns in a resident-resident chat
    agent_chat_cooldown: int = 1800        # seconds before same pair can chat again
    agent_time_scale: float = 1.0          # world time multiplier (1.0 = realtime)
    agent_enabled: bool = True             # master switch (set False to pause loop)
    agent_debug_always_active: bool = False  # bypass schedule, all residents always active

    # --- Lab / experiment building (元游戏入口) ---
    # Deploy-level master switch (loaded at startup). The *runtime* kill switch
    # is the Redis flag ``sv:lab:enabled`` (admin toggles it live, no restart);
    # this only gates whether the feature is wired at deploy time.
    lab_enabled: bool = False
    lab_adapter: str = "mock"               # default sandbox adapter (mock|openclaw|hermes|computer_use)
    lab_creator_share: float = 0.2          # researcher's creator gets this share of reward_sc; rest → treasury
    lab_platform_fee_rate: float = 0.1      # platform fee added on top of reward (fee = ceil(reward*rate)) → sink
    lab_max_concurrent_runs: int = 3        # global cap on concurrently-running runs
    lab_daily_tasks_per_user: int = 20      # per-player daily task-publish cap
    lab_default_budget_usd: float = 0.5     # per-run LLM/compute budget ceiling
    lab_sc_per_usd: int = 100               # SC↔USD conversion (price scopes / validate reward vs budget)
    lab_approval_timeout_s: int = 1800      # sensitive-action human-review timeout (default: deny)
    lab_run_heartbeat_ttl_s: int = 300      # orphan-run watchdog threshold (no heartbeat past this → reap+refund)
    lab_auto_release_hours: int = 72        # review→auto-release window (anti-runaway)
    lab_task_deadline_hours: int = 24       # default task deadline if the issuer doesn't set one

    # --- Rate Limiting (OPTIMIZATION_PLAN P1-1, limit sub-item) ---
    # WS chat_msg sliding window is in-process (single-worker model); REST uses
    # slowapi. Both migrate to Redis once P0-3b lands the cross-process bus.
    ws_rate_limit_per_minute: int = 20          # chat_msg per user per minute
    rest_rate_limit_register_per_minute: int = 5   # auth register/login (by IP)
    rest_rate_limit_forge_per_minute: int = 10     # forge start/quick (by IP)
    rest_rate_limit_llm_test_per_minute: int = 5   # settings/llm/test (by IP)

    # --- Observability (OPTIMIZATION_PLAN P1-3) ---
    # metrics_enabled / sentry_* live in the Observability block above (a
    # duplicate set of definitions was removed in PLAN_P3 批次 3).
    slow_query_ms: int = 0        # log SQL slower than N ms (0 = disabled)

    model_config = {"env_file": ".env"}


settings = Settings()
