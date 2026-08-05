from pydantic import Field, model_validator
from pydantic_settings import BaseSettings

_DEFAULT_JWT_SECRET = "dev-secret-change-in-production"
# Known placeholders that must never reach production, even though they are
# not the app's own default constant (e.g. the deploy template ships a
# different placeholder string than the code's dev default — P0-4b hardening
# after security audit: an exact-constant-only check let that one through).
_PLACEHOLDER_JWT_SECRETS = {
    _DEFAULT_JWT_SECRET,
    "generate-a-64-char-random-string-here",  # deploy/backend/.env.example
}
_MIN_JWT_SECRET_LENGTH = 32


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
        if not self.debug and (
            self.jwt_secret in _PLACEHOLDER_JWT_SECRETS
            or len(self.jwt_secret) < _MIN_JWT_SECRET_LENGTH
        ):
            raise ValueError(
                "JWT_SECRET is missing, a known placeholder, or too short "
                f"(<{_MIN_JWT_SECRET_LENGTH} chars) — refusing to start. Set "
                "JWT_SECRET to a long random string, or set DEBUG=true for "
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
    # Default raised to 10.0 to fund the world-clock k=4 speed-up: daily-action
    # counting now resets per WORLD day (tick.py._daily_key), so a resident can
    # act cap×4 ≈ 80×/real-day ≈ $6/real-day for a 15-resident world — kept under
    # this $10 guardrail. Raise for larger worlds. Set 0 to disable.
    budget_global_daily_usd: float = 10.0
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
    # Pixel-art post-processing (Image-to-Pixel style grid snap)
    portrait_pixel_grid: int = 64
    portrait_pixel_colors: int = 32

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

    # --- Static files & Media Upload (P2) ---
    # Root directory served at GET /static (portraits, uploaded media). Relative
    # paths resolve against the process CWD: `backend/` in dev (uvicorn runs
    # there), `/app` in the Docker image — both yield <cwd>/static.
    static_dir: str = "static"
    # Durable, non-public working tree produced by the resident sprite worker.
    # Admin review/publish APIs reject every candidate path outside this root.
    resident_sprite_enabled: bool = False
    resident_sprite_artifact_dir: str = "var/resident-sprites"
    resident_sprite_provider_base_url: str = ""
    resident_sprite_provider_api_key: str = ""
    resident_sprite_provider_model: str = "gpt-image-2"
    resident_sprite_provider_timeout: float = 180.0
    # Test-only opt-in. Never enable for production traffic or credentials.
    resident_sprite_allow_insecure_http_test: bool = False
    # Conservative operator-supplied upper bound, not provider billing data.
    # Zero means unknown; the admin API/UI must not present it as zero spend.
    resident_sprite_request_cost_upper_bound_usd: float = Field(default=0.0, ge=0.0)
    resident_sprite_capability_receipt: str = ""
    resident_sprite_revocation_root: str = ""
    resident_sprite_worker_poll_seconds: float = 5.0
    resident_sprite_worker_lease_seconds: int = 7200
    # Media uploads live UNDER static_dir so they are reachable at
    # /static/uploads/... . (The old default "backend/static/uploads" pointed
    # outside the served root in Docker, so every upload URL 404'd.)
    media_upload_dir: str = "static/uploads"
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
    agent_max_daily_actions: int = 20      # per WORLD day (accelerated by WORLD_CLOCK_K); a spend guardrail, not real-time
    agent_chat_max_turns: int = 8          # max dialog turns in a resident-resident chat
    agent_chat_cooldown: int = 1800        # seconds before same pair can chat again
    agent_enabled: bool = True             # master switch (set False to pause loop)
    agent_debug_always_active: bool = False  # bypass schedule, all residents always active

    # --- World Clock (agent-T) — single conversion entry lives in app/world_clock.py ---
    # World time = WORLD_EPOCH + k×(real elapsed). k=4 → 1 real day = 4 world days
    # (a full day/night every 6 real hours). Residents' 作息/星期/日期语义 read world
    # time; LLM budget日结/cron/TTL/日志 stay on real time. See WORLD_CLOCK_DESIGN.md.
    world_clock_k: int = 4                                    # world-time speed multiplier
    world_epoch: str = "2026-01-01T00:00:00+08:00"           # instant where world==real (fixed, tz-aware Asia/Shanghai)
    timezone: str = "Asia/Shanghai"                          # time-semantics anchor zone (UTC+8, no DST)

    # --- Lab / experiment building (元游戏入口) ---
    # Deploy-level master switch (loaded at startup). The *runtime* kill switch
    # is the Redis flag ``sv:lab:enabled`` (admin toggles it live, no restart);
    # this only gates whether the feature is wired at deploy time.
    lab_enabled: bool = False
    lab_adapter: str = "mock"               # default sandbox adapter (mock|openclaw|hermes|computer_use)
    lab_creator_share: float = 0.2          # researcher's creator gets this share of reward_sc; rest → treasury
    lab_platform_fee_rate: float = 0.1      # platform fee added on top of reward (fee = ceil(reward*rate)) → sink
    lab_max_concurrent_runs: int = 3        # global cap on concurrently-running runs
    lab_max_concurrent_per_researcher: int = 1  # per-researcher cap on concurrently-running runs (0 = disabled)
    lab_daily_tasks_per_user: int = 20      # per-player daily task-publish cap
    lab_default_budget_usd: float = 0.5     # per-run LLM/compute budget ceiling
    lab_sc_per_usd: int = 100               # SC↔USD conversion (price scopes / validate reward vs budget)
    lab_approval_timeout_s: int = 1800      # sensitive-action human-review timeout (default: deny)
    lab_run_heartbeat_ttl_s: int = 300      # orphan-run watchdog threshold (no heartbeat past this → reap+refund)
    lab_auto_release_hours: int = 72        # review→auto-release window (anti-runaway)
    lab_task_deadline_hours: int = 24       # default task deadline if the issuer doesn't set one
    lab_task_blocklist: list[str] = []      # operator-supplied content blocklist for task title/brief moderation
    # P2 real sandbox: one-shot container image + default-deny egress allowlist.
    lab_sandbox_image: str = ""             # container image for isolated runs ("" = not provisioned)
    lab_egress_allowlist: list[str] = []    # allowed egress hosts (e.g. ["*.wikipedia.org"])
    # P2-E rootless OCI executor: only R1 code/shell tools route through it, and
    # only when both the flag is on AND an image is set. Off = Mock _mock_executor
    # (default path, zero change). macOS+colima evidence is dev-grade; a prod gate
    # needs a dedicated Linux runner (cgroup v2 + rootless + seccomp/AppArmor).
    lab_oci_enabled: bool = False
    lab_oci_image: str = ""                 # OCI executor image ("" = disabled even if the flag is on)
    # Real adapter endpoints — empty string = unconfigured (portrait/tts grouping
    # convention). The adapter still imports; start() raises LabAdapterUnconfigured.
    lab_simverse_ref_base_url: str = ""     # Simverse reference runtime (Phase 7 selected candidate) endpoint
    lab_simverse_ref_api_key: str = ""
    lab_openclaw_base_url: str = ""
    lab_openclaw_api_key: str = ""
    lab_hermes_base_url: str = ""
    lab_hermes_api_key: str = ""
    lab_computer_use_base_url: str = ""
    lab_computer_use_api_key: str = ""
    # Operator-supplied commercial runtime endpoint (P7 candidate). Anthropic-
    # messages-compatible; used to score a second real candidate through the gate.
    agent_base_url: str = ""
    agent_api_key: str = ""
    agent_model: str = ""

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

    # --- Lab Agent v1 (P0/P1 protocol + safety slice) ---
    lab_agent_v1_enabled: bool = False      # feature flag: new grant/policy/broker/ledger path (off = legacy mock path)
    lab_grant_secret: str = ""              # HMAC secret for run grants ("" = fall back to jwt secret)
    lab_grant_ttl_s: int = 900              # grant TTL (PRD: 15 min)
    lab_policy_version: str = "lab-policy-v1"
    lab_budget_model_tokens: int = 200_000
    lab_budget_tool_calls: int = 100
    lab_budget_wall_clock_ms: int = 1_200_000   # 20 min
    lab_budget_egress_requests: int = 200
    lab_budget_egress_bytes: int = 104_857_600  # 100 MiB
    lab_budget_artifact_count: int = 20
    lab_budget_artifact_bytes: int = 104_857_600
    lab_budget_active_workers: int = 3
    lab_artifact_retention_days: int = 30   # V12: expires_at = finalized_at + this; retention_hold pins evidence

    # --- Lab Agent protocol v2 rollout (Approved v10, default-deny) ---
    # Each trust plane has an independent gate. A closed v2 gate never permits
    # fallback to protocol v1 or Mock execution. The Lab Runner always performs
    # v1 command/event recovery; the worker gate admits its dedicated v2 consumer.
    lab_agent_v2_enabled: bool = False
    lab_terminalizer_v2_enabled: bool = False
    lab_terminalizer_worker_enabled: bool = False
    lab_terminalizer_database_url: str = ""
    lab_outbox_v2_enabled: bool = False
    lab_runtime_v2_canary_enabled: bool = False
    lab_global_admission_enabled: bool = False
    lab_service_sha: str = ""
    lab_d0_release_receipt_path: str = ""
    lab_d0_release_receipt_sha256: str = ""
    lab_d0_request_hash: str = ""
    # Gateway-only Runtime audience signing material. These never fall back to
    # the legacy adapter API key or the application JWT secret.
    lab_runtime_auth_issuer: str = ""
    lab_runtime_auth_audience: str = "lab-runtime"
    lab_runtime_auth_current_kid: str = ""
    lab_runtime_auth_current_key: str = ""
    lab_runtime_auth_next_kid: str = ""
    lab_runtime_auth_next_key: str = ""
    lab_runtime_auth_token_ttl_s: int = 300
    lab_runtime_v2_canary_tenants: list[str] = []

    # Remote Executor. The Runner never receives an OCI socket; it submits one
    # fenced job to this independently deployed service.
    lab_executor_enabled: bool = False
    lab_executor_base_url: str = ""
    lab_executor_image_digest: str = ""
    lab_executor_auth_issuer: str = ""
    lab_executor_auth_audience: str = "lab-executor"
    lab_executor_auth_current_kid: str = ""
    lab_executor_auth_current_key: str = ""
    lab_executor_auth_next_kid: str = ""
    lab_executor_auth_next_key: str = ""
    lab_executor_auth_token_ttl_s: int = 300
    lab_executor_receipt_issuer: str = ""
    lab_executor_receipt_audience: str = "lab-executor-receipt"
    lab_executor_receipt_current_kid: str = ""
    lab_executor_receipt_current_key: str = ""
    lab_executor_receipt_next_kid: str = ""
    lab_executor_receipt_next_key: str = ""
    lab_executor_request_timeout_s: float = 30.0
    lab_executor_poll_interval_s: float = 0.5
    lab_executor_job_timeout_s: int = 120
    lab_executor_job_cpu_millis: int = 2_000
    lab_executor_job_memory_bytes: int = 1_073_741_824
    lab_executor_job_pids: int = 512
    lab_executor_job_stdout_bytes: int = 65_536
    lab_executor_job_stderr_bytes: int = 65_536
    lab_executor_job_scratch_bytes: int = 536_870_912

    # Production Artifact pipeline. These are independent trust planes and
    # intentionally do not fall back to DB blobs or arbitrary Runtime URIs.
    lab_artifact_pipeline_enabled: bool = False
    lab_artifact_ingest_base_url: str = ""
    lab_artifact_scanner_base_url: str = ""
    lab_artifact_cleanup_base_url: str = ""
    lab_artifact_upload_lease_ttl_s: int = 300
    lab_artifact_upload_max_attempts: int = 5
    lab_artifact_pending_ttl_hours: int = 24
    lab_artifact_quarantine_ttl_days: int = 7
    lab_artifact_service_timeout_s: float = 30.0
    lab_artifact_scan_poll_interval_s: float = 1.0
    lab_artifact_scan_deadline_s: int = 900
    lab_artifact_scan_policy_version: str = "lab-artifact-scan-v1"
    lab_artifact_scan_max_attempts: int = 5
    lab_artifact_cleanup_max_attempts: int = 10
    lab_artifact_inline_max_bytes: int = 1_048_576
    # API-side released-object reader. These credentials/mounts must be
    # read-only and must not grant access to the quarantine bucket.
    lab_artifact_download_backend: str = ""
    lab_artifact_download_storage_root: str = ""
    lab_artifact_download_released_bucket: str = ""
    lab_artifact_download_s3_endpoint_url: str = ""
    lab_artifact_download_s3_region: str = ""
    lab_artifact_download_s3_access_key: str = ""
    lab_artifact_download_s3_secret_key: str = ""
    lab_artifact_download_s3_session_token: str = ""
    lab_artifact_download_timeout_s: float = 30.0
    lab_artifact_download_max_bytes: int = 104_857_600
    lab_artifact_allowed_mime_types: list[str] = [
        "application/json",
        "application/pdf",
        "application/zip",
        "image/jpeg",
        "image/png",
        "text/csv",
        "text/markdown",
        "text/plain",
    ]
    lab_artifact_ingest_auth_issuer: str = ""
    lab_artifact_ingest_auth_audience: str = "lab-artifact-ingest"
    lab_artifact_ingest_auth_current_kid: str = ""
    lab_artifact_ingest_auth_current_key: str = ""
    lab_artifact_ingest_auth_next_kid: str = ""
    lab_artifact_ingest_auth_next_key: str = ""
    lab_artifact_scanner_auth_issuer: str = ""
    lab_artifact_scanner_auth_audience: str = "lab-artifact-scanner"
    lab_artifact_scanner_auth_current_kid: str = ""
    lab_artifact_scanner_auth_current_key: str = ""
    lab_artifact_scanner_auth_next_kid: str = ""
    lab_artifact_scanner_auth_next_key: str = ""
    lab_artifact_cleanup_auth_issuer: str = ""
    lab_artifact_cleanup_auth_audience: str = "lab-artifact-cleanup"
    lab_artifact_cleanup_auth_current_kid: str = ""
    lab_artifact_cleanup_auth_current_key: str = ""
    lab_artifact_cleanup_auth_next_kid: str = ""
    lab_artifact_cleanup_auth_next_key: str = ""
    # JSON object of receipt issuer -> {kid: verification key}. Production
    # values are mounted from the approved trust root, never from JWT_SECRET.
    lab_artifact_ingest_receipt_issuer: str = ""
    lab_artifact_scanner_receipt_issuer: str = ""
    lab_artifact_cleanup_receipt_issuer: str = ""
    lab_artifact_receipt_algorithm: str = "EdDSA"
    lab_artifact_receipt_keys_json: str = "{}"

    # --- Realism (world simulation; REALISM_*, master switch below) ---
    # Master switch. Default False → behavior identical to pre-realism. Deploys
    # opt in via REALISM_ENABLED=true for burn-in A/B and easy rollback.
    realism_enabled: bool = False
    # P1 movement (defined now so REALISM_MOVE_SPEED env parses; used in P1).
    realism_move_speed: int = 8
    # Task 2 retrieval scoring (Generative-Agents weights; must sum to 1.0).
    realism_retrieval_relevance_weight: float = 0.45
    realism_retrieval_recency_weight: float = 0.30
    realism_retrieval_importance_weight: float = 0.25
    realism_recency_tau_hours: float = 72.0   # τ base; τ = this × (1 + importance)
    # Task 2 eviction (soft-archive) thresholds.
    realism_evict_importance_floor: float = 0.35
    realism_evict_idle_days: int = 90
    # Task 5b recycling: approved proposal stuck longer than this → fail+refund.
    realism_proposal_stuck_minutes: int = 10
    # Task 3 mood write-back deltas (positive / negative chat outcome).
    realism_mood_positive_valence: float = 0.15
    realism_mood_positive_arousal: float = 0.05
    realism_mood_negative_valence: float = -0.2
    realism_mood_negative_arousal: float = 0.1
    # Task 3 flashbulb: importance += coef × |valence| × arousal.
    realism_flashbulb_coef: float = 0.2

    # --- Realism P1 (rule-based realism; still gated by realism_enabled) ---
    # Task 7 movement modulation (multiplicative on base speed).
    realism_move_rain: float = 0.75
    realism_move_storm: float = 0.5
    realism_move_snow: float = 0.6
    realism_move_arousal_boost: float = 1.2   # applied when arousal > threshold
    realism_move_arousal_threshold: float = 0.7
    # Task 8 weather → activity probability multiplier.
    realism_weather_sunny: float = 1.0
    realism_weather_cloudy: float = 0.95
    realism_weather_rain: float = 0.7
    realism_weather_storm: float = 0.4
    realism_weather_snow: float = 0.75
    realism_shelter_prob: float = 0.6         # P(outdoor resident reroutes indoors in rain/storm)
    realism_weather_mood_rain_valence: float = -0.02
    realism_weather_mood_rain_arousal: float = -0.01
    realism_weather_mood_sunny_valence: float = 0.02
    # Task 9 weekday/festival.
    realism_weekend_wake_delay: int = 1       # weekend wake_hour += this
    realism_weekend_rest_boost: float = 0.1
    realism_festival_social_boost: float = 0.2
    realism_festival_weight: float = 3.0
    # Task 10 needs metabolism (per tick unless noted).
    realism_needs_initial: float = 0.8
    realism_needs_critical: float = 0.25
    realism_energy_awake: float = -0.004
    realism_energy_walking: float = -0.006
    realism_energy_sleep: float = 0.02
    # 0804 重校准：k=4 下 1 世界日=360 轮 60s tick，默认作息 metabolize ≈
    # 84(清醒,should_tick 门控)+150(睡眠,每轮) ≈234 次/日。旧 -0.005 日扣减
    # 1.17，任何可达的 EAT 频率(+0.5/次)都补不回 → satiety 全员锁死 0。
    # -0.0015 → 0.35/日：~1.4 世界日一次 EAT 即可维持（critical<0.25 强制
    # EAT 环路真实可达），稳态在 0.25–0.75 震荡。
    realism_satiety_decay: float = -0.0015
    realism_eat_restore: float = 0.5
    realism_social_introvert: float = -0.001
    realism_social_extravert: float = -0.006
    realism_social_default: float = -0.003
    realism_social_chat: float = 0.4
    realism_social_greet: float = 0.1
    # Task 11 emotion loop.
    realism_goal_achieved_valence: float = 0.4
    realism_goal_achieved_arousal: float = 0.2
    realism_goal_failed_valence: float = -0.3
    realism_goal_failed_arousal: float = 0.1
    realism_gossip_victim_valence: float = -0.1
    realism_gossip_victim_arousal: float = 0.15
    realism_dream_tone_delta: float = 0.1
    realism_valence_activity_coef: float = 0.2   # activity ×= (1 + coef×valence)
    realism_contagion_rate: float = 0.1          # v += rate × (mean − v)
    # Task 12 importance calibration.
    realism_importance_window: int = 100
    realism_shift_percentile: float = 0.95       # shift gate: normalized ≥ P95 ...
    realism_shift_valence_gate: float = 0.5      # ... AND |valence| > this


    # --- Realism P2 (social structure; three INDEPENDENT switches, all False) ---
    # Each gate is independent of realism_enabled and of each other, so relations,
    # information gradient and crowd can be A/B'd separately during burn-in. Any
    # one False → the corresponding path behaves exactly as pre-P2.
    realism_relations_enabled: bool = False
    realism_info_gradient_enabled: bool = False
    realism_crowd_enabled: bool = False
    # P2 Task 1 — relation write deltas (reused, zero new LLM calls) + decay.
    realism_rel_familiarity_chat: float = 0.05
    realism_rel_affinity_chat: float = 0.03      # ± by wrapup mood (positive/negative)
    realism_rel_familiarity_witness: float = 0.01
    realism_rel_affinity_gift: float = 0.1
    realism_rel_affinity_invest: float = 0.1
    realism_rel_decay_idle_days: int = 30        # no interaction for this long → decay
    realism_rel_familiarity_decay: float = 0.95  # ×/week on idle relations
    realism_rel_affinity_decay: float = 0.98     # ×/week (2% regression toward 0)
    # P2 Task 3 — read-path weighted sampling.
    realism_rel_encounter_fam_coef: float = 2.0  # encounter weight = 1 + coef×familiarity
    realism_rel_chat_epsilon: float = 0.1        # ε uniform mix in CHAT target sampling
    realism_rel_gossip_fam_floor: float = 0.1    # gossip candidate weight = floor + familiarity(subject)
    # P2 Task 4 — circle detection (connected components over strong ties).
    realism_circle_threshold: float = 0.3        # familiarity ≥ this = a "strong" edge
    # P2 Task 5 — information gradient (differentiated event awareness).
    realism_info_geo_radius: int = 15            # tiles: residents within this of the event location
    realism_info_sample_frac: float = 0.2        # random "well-informed" first-hand sample of the rest
    realism_info_geo_importance: float = 0.6     # first-hand importance for geo-related residents
    realism_info_sample_importance: float = 0.5  # first-hand importance for the random sample
    # P2 Task 7 — crowd / 人流聚集 (festival draw + herd micro-rule).
    realism_festival_location: str = "central_plaza"  # default gathering place for a location-less festival
    realism_crowd_threshold: int = 5             # a location with ≥ this many residents reads as "lively"
    realism_crowd_social_max: float = 0.5        # herd hint only when own social need < this
    # (festival ×3 draw reuses realism_festival_weight defined above)

    # ── Town extension milestones (M1–M6) — each an independent gate ────
    # M1 economy: duty wages, NPC meal cost, wallet-pressure hint, resident works.
    npc_economy_enabled: bool = True
    npc_default_wage_sc: int = 5                  # duty wage when perks lack wage_sc
    npc_meal_cost_sc: int = 2                     # EAT debit from the resident's treasury
    npc_wallet_pressure_threshold: int = 3        # below this balance → "手头紧" prompt hint
    npc_work_item_prob: float = 0.35              # chance a producing WORK also lists a shop item
    npc_work_item_price_sc: int = 15              # price of a resident-made shop item
    npc_work_item_stock: int = 3                  # limited stock per listing
    market_day_weekday: int = 5                   # Saturday=5: weekly 集市日
    market_day_discount: float = 0.9              # shop price × this on market day
    # M2 story arcs: rule-triggered milestone engine (nightly, zero tick cost).
    arc_engine_enabled: bool = True
    # M3 civic governance: proposals → clerk bulletin → NPC+player vote → execute.
    civic_polls_enabled: bool = True
    civic_poll_days: int = 3                      # voting window length
    # Kill switch for the 2026-07-25 _npc_choice bias fix (option-0 monopoly:
    # A2=M zero signal + index-order tie-break + all-effect polls). True falls
    # back to the pre-fix scorer byte-for-byte. Default False = fix ON, because
    # a default-off bug fix leaves production broken.
    civic_npc_choice_legacy: bool = False
    # M6 seasonal mayor election (built on the M3 engine).
    election_enabled: bool = True
    election_mayor_wage_bonus: float = 1.2
    election_interval_days: int = 28              # off-season election cadence (nightly trigger)        # winner's town-wide wage multiplier

    # E9 辩论擂台生命周期推进（event_cron 每 60s 一轮）。两个窗口都从
    # Debate.starts_at 起算——debates 表没有记录进入 voting 时刻的列，不动
    # schema 的前提下 settle 的判据只能是 stake_window + vote_window 之和。
    debate_stake_window_min: int = 30    # announced 满这么久 → 开打（run_live）
    debate_vote_window_min: int = 60     # voting 满这么久 → 结算（settle）
    debate_stuck_hours: int = 24         # 卡在非终态超过这么久 → 平局全额退款

    # E12/C3 赛季：自动开季的季长（真实日）。seasons 表长期 0 行，导致
    # season_service.add_points 的第一行 `if not season_id: return 0` 把所有
    # 积分静默丢弃 —— 读端和记分端都在，缺的只是写端。
    #
    # 默认 28，与下面的 election_interval_days 对齐：SEASON_AUTO_OPEN 默认开、
    # 部署即生效，开季后 election_service.maybe_open_seasonal_election 的
    # season 分支会永久接管镇长选举节奏（从 election_interval_days 改成读
    # season_length_days）；同时 season_service.settle_season 的赛季结算
    # top-3 派彩（200/120/80 SC）首次可达，季越短派彩越频繁。28 让选举节奏
    # 维持现状（不被开季顺带减半），派彩频率先按季观察，不在这次改动里
    # 顺带调快。
    #
    # 下界 1：ensure_active_season 会把 <=0 的值 clamp 到 1 ——一个 env 打字
    # 错误（0 或负数）本会让 ends_at<=starts_at，下一个 60s tick 就把这季
    # 判定到期结算，同一 tick 又开一季，形成每 60 秒一条置顶落幕公告的失控
    # 循环；clamp 之后最坏情况退化成"季长 1 天"而不是"每分钟开一季"。
    season_length_days: int = 28
    season_auto_open: bool = True        # 关掉则只能由 admin 手动开季

    # ── S2-1 offices 职位实体化 (POLIS_OFFICE_*) — independent gate ─────
    # Unified offices table (mayor/town_clerk/postman/doctor) + OfficeService.
    # Default False (rollback-safe, realism-family pattern): off → byte-level
    # fallback to today's install_mayor / current_mayor / _pay_wage /
    # find_duty_resident behavior; the offices table is neither read nor
    # written by any business path and term_check is skipped in the cron.
    polis_office_enabled: bool = False
    # Mayor term length in WORLD days (world_clock is the only conversion
    # entry point). 0 = unlimited term — byte-equivalent to today's
    # overwrite-on-reelection mayor; >0 enables nightly term expiry.
    polis_office_mayor_term_days: int = 0
    # ── F2 公民权晋升／撤销 (CIVIC_*) — ROADMAP #5 收口注册 ────────────────
    # 读点在 app/services/civic_membership.py：**调用时**先读进程 env（F2 的
    # 近百条测试按用例 monkeypatch.setenv 改档位/门槛，赖此成立），env 未设时
    # 经 _settings_default 落到这里。默认值必须与 civic_membership 的代码默认
    # 逐字一致（tests/test_civic_settings_knobs.py 钉住）。
    # 三态闸门：off=零读零写；shadow=只算名单不写库；on=真晋升（且被
    # assert_thresholds_calibrated 挡住未标定的占位门槛）。默认 off，开闸是
    # 独立一次变更（红线：行为开闸与代码变更分开）。
    civic_promotion_mode: str = "off"
    civic_auto_demotion_enabled: bool = False
    # ⚠️ 三个门槛是占位值不是标定值——mode=on 前必须先跑
    # scripts/civic_calibration_report.py 用真实分布标定。
    civic_promotion_min_world_days: float = 30.0
    civic_promotion_min_peers: int = 3
    civic_promotion_min_familiarity: float = 0.20
    civic_peer_seasoning_world_days: float = 28.0
    civic_promotion_max_per_run: int = 5
    civic_promotion_breaker_fraction: float = 0.20
    civic_promotion_breaker_min_abs: int = 3
    civic_min_electorate: int = 3
    civic_min_tenure_world_days: float = 12.0
    civic_promotion_cooldown_world_days: float = 12.0
    # ── S1-3 议题立场与舆论动力学 (KICKOFF_S1-3_opinion.md §3) ─────────────
    # Independent gate, default False → byte-identical fallback to the status
    # quo (debate/chat/digest/nightly paths unchanged, zero stance writes).
    # All knobs share the POLIS_OPINION_ env prefix; zero new LLM calls.
    polis_opinion_enabled: bool = False        # 主开关 (env POLIS_OPINION_ENABLED)
    polis_opinion_epsilon: float = 0.4         # 有界信任阈值 ε (|Δstance|<=ε 才互相影响)
    polis_opinion_chat_rate: float = 0.08      # from_chat Deffuant 步长
    polis_opinion_drift_rate: float = 0.05     # nightly 漂移步长
    polis_opinion_seed_mag: float = 0.3        # 辩论开场初始对立幅度(缺 SBTI 时)
    polis_opinion_active_window_days: int = 14 # "活跃议题"时间窗(世界日, 经 world_clock 换算)
    polis_opinion_min_participants: int = 3    # "活跃议题"最少表态人数
    polis_opinion_neg_repel: bool = False      # negative mood 是否轻微远离(默认仅"不靠拢")
    polis_opinion_digest_issues: int = 2       # 日报 opinion_line 最多点名的议题数
    polis_opinion_variance_split: float = 0.15 # opinion_line 措辞阈值: 方差≥此值读作"分歧"
    # ── S1-1 公共声誉轴 (REP_*) ───────────────────────────────────────
    # Default False keeps every existing read/write path byte-equivalent.
    # Nightly aggregation is pure-rule and adds no LLM calls or migrations.
    rep_enabled: bool = False
    rep_min: float = -1.0
    rep_max: float = 1.0
    rep_neutral: float = 0.0
    rep_ema_alpha: float = 0.3
    rep_gossip_base_tone: float = -0.3
    rep_distortion_penalty: float = -0.2
    rep_mood_weight: float = 0.2
    rep_vote_trust_weight: float = 1.0
    rep_credit_min_score: float = -0.3

    # ── S1-5 镇财政闭环 (KICKOFF_S1-5_treasury.md §3, TOWN_* env prefix) ────
    # Independent gate, default False → byte-level fallback to the status quo:
    # wages keep being MINTED from nothing, resident sales are untaxed, the
    # nightly public-spending job is skipped whole, and no treasury_changed WS
    # event is emitted. Pure rules, zero new LLM calls.
    town_treasury_enabled: bool = False         # 主开关 (env TOWN_TREASURY_ENABLED)
    town_tax_rate_sales: float = 0.1            # 居民售货销售税率(skim 进镇财政)
    town_tax_rate_gift: float = 0.0             # 送礼/打赏分成税率,默认 0(留旋钮)
    town_wage_unfunded_policy: str = "skip"     # 镇财政见底: skip=欠薪 / mint=回落凭空铸造
    town_public_works_daily_sc: int = 0         # nightly 公共支出预算,0=只做对账不拨款
    town_ws_min_delta_sc: int = 0               # treasury_changed 广播阈值,0=不广播

    # ── S2-5 policies + 四级分级审批 (POLIS_POLICY_*) ────────────────────
    # 两个独立门, 都默认 False → 行为与现状字节级一致 (KICKOFF_S2-5 §3):
    #   polis_policy_enabled=False          → PolicyService.get/get_group 回落
    #       ConfigService(system_config), seed_defaults 不执行, policies 表不被
    #       任何业务路径读写, /admin/policies 空态。
    #   polis_policy_approval_enabled=False → proposal_service.approve_proposal
    #       不插 tier 门 (回落单-admin CAS), civic_service._close_one 回落纯
    #       plurality (无阈值/无法定人数), _execute_outcome 不识别 policy 效果
    #       类型 (未知类型按现状 no-op)。
    # 可以只开存储门做影子读写, 再开审批门接四级路由。零新增 LLM 调用。
    polis_policy_enabled: bool = False           # 主开关: policies 表读写路径总门
    polis_policy_approval_enabled: bool = False  # 独立门: 四级审批路由 (track A/B)
    polis_policy_simple_majority_threshold: float = 0.50    # 简单多数阈值
    polis_policy_absolute_majority_threshold: float = 0.667 # 绝对多数阈值(超多数)
    polis_policy_quorum_fraction: float = 0.50   # 绝对多数档的法定投票人数占比

    # ── 工程健康批 (2026-07-25B 收口登记) ────────────────────────────────
    # 这两组旋钮在运行时由 app/services/social_status_recovery.py 与
    # app/tasks/loop_heartbeat.py 直接读 os.environ (运维热改、无需重启);
    # 这里的字段是它们的**默认值来源**——env 未设时回落到这些值,同时让
    # .env.example 的每个 key 都能映射到真实 Settings 字段
    # (tests/test_env_example_consistency.py 不变量 1)。
    # R4 聊天锁 DB 侧回收: worker 猝死留下的 socializing 状态的回收器。
    social_status_recovery_enabled: bool = True
    social_status_stale_seconds: int = 600       # = ws SOCIAL_LOCK_TTL
    # P2 后台 loop 心跳与死亡告警 (默认开, 一键可静默)。
    loop_heartbeat_enabled: bool = True
    loop_heartbeat_stale_factor: float = 3.0     # 过期阈值 = N × 该 loop 自身节拍
    loop_heartbeat_min_stale_sec: float = 900.0  # 阈值下限, 防 60s 级 loop 误报
    loop_heartbeat_alert_cooldown_min: float = 60.0   # 同 loop 两次告警最小间隔
    loop_heartbeat_check_interval_min: float = 5.0    # 一次 beat 最多多久巡检一次

    model_config = {"env_file": ".env"}


settings = Settings()
