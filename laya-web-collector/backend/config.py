"""Runtime configuration for the real-pilot web collector.

Secrets are read from environment variables only.  Cloud deployments should
inject them through the container runtime's secret manager, never through Git.

In ``APP_ENV=production`` the application refuses to start unless the
Supabase-hosted configuration described in
``deploy/SUPABASE_MIGRATION_GUIDE.md`` is in place; see
``validate_production_settings`` and ``backend.db.check_database_schema``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


APP_DIR = Path(__file__).resolve().parents[1]
_load_dotenv(APP_DIR / ".env")


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip() == "1"


def _default_db_auto_create() -> bool:
    """SQLite keeps create_all for local dev; Postgres must use migrations."""
    url = os.getenv("DATABASE_URL", "")
    if not url:
        return True
    return url.startswith("sqlite")


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "development")
    database_url: str = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{APP_DIR / 'local_pilot.sqlite3'}",
    )
    export_dir: Path = Path(os.getenv("EXPORT_DIR", str(APP_DIR / "exports")))

    # --- Postgres / Supabase connection management (W2) -------------------
    # ``DB_AUTO_CREATE`` defaults to 1 for SQLite and 0 for Postgres so that
    # production never races two uvicorn workers through ``create_all``.
    db_auto_create: bool = _env_flag("DB_AUTO_CREATE", _default_db_auto_create())
    db_pool_size: int = int(os.getenv("DB_POOL_SIZE", "5"))
    db_max_overflow: int = int(os.getenv("DB_MAX_OVERFLOW", "2"))
    db_expected_schema: str = os.getenv("DB_EXPECTED_SCHEMA", "pilot")

    # --- Supabase (server-side only) --------------------------------------
    supabase_url: str = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_publishable_key: str = (
        os.getenv("SUPABASE_PUBLISHABLE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    )
    # The secret / service_role key must never reach the browser.
    supabase_secret_key: str = (
        os.getenv("SUPABASE_SECRET_KEY", "") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    )
    supabase_jwt_secret: str = os.getenv("SUPABASE_JWT_SECRET", "")

    # --- Auth / storage backends ------------------------------------------
    auth_mode: str = os.getenv("AUTH_MODE", "disabled")
    auth_gateway_secret: str = os.getenv("AUTH_GATEWAY_SECRET", "")
    storage_backend: str = os.getenv("STORAGE_BACKEND", "local")
    storage_bucket: str = os.getenv("STORAGE_BUCKET", "pilot-private")
    # Data-governance switches (D1 / D5).
    allow_real_patient_data: bool = _env_flag("ALLOW_REAL_PATIENT_DATA", False)
    llm_persist_raw: bool = _env_flag("LLM_PERSIST_RAW", True)

    llm_base_url: str = os.getenv(
        "LLM_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_question_model: str = os.getenv("LLM_QUESTION_MODEL", "qwen3.8-max")
    llm_extractor_model: str = os.getenv("LLM_EXTRACTOR_MODEL", "qwen3.8-flash")
    llm_timeout_seconds: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "90"))
    llm_max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "2048"))
    llm_temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.1"))
    llm_top_p: float = float(os.getenv("LLM_TOP_P", "1.0"))
    llm_enable_thinking: bool = os.getenv("LLM_ENABLE_THINKING", "0") == "1"
    llm_json_mode: bool = os.getenv("LLM_JSON_MODE", "1") == "1"

    cors_origins: str = os.getenv(
        "CORS_ORIGINS",
        "http://127.0.0.1:8080,http://localhost:8080",
    )
    auto_seed_demo: bool = os.getenv("AUTO_SEED_DEMO", "1") == "1"
    # Randomization.  A fixed global seed made the allocation sequence derivable
    # from public identifiers, so it is no longer used as the per-encounter seed.
    # ``randomization_deterministic_seed`` exists only for reproducible local
    # tests and must stay unset in the pilot.
    randomization_deterministic_seed: int | None = (
        int(os.environ["RANDOMIZATION_DETERMINISTIC_SEED"])
        if os.getenv("RANDOMIZATION_DETERMINISTIC_SEED")
        else None
    )
    randomization_strategy: str = os.getenv("RANDOMIZATION_STRATEGY", "stratified_simple")
    randomization_block_sizes: str = os.getenv("RANDOMIZATION_BLOCK_SIZES", "3,6")
    randomization_schedule_version: str = os.getenv(
        "RANDOMIZATION_SCHEDULE_VERSION",
        "target-fact-stratified-simple-v2",
    )
    randomization_target_per_arm: int = int(os.getenv("RANDOMIZATION_TARGET_PER_ARM", "30"))
    adjudication_reviews_required: int = int(os.getenv("ADJUDICATION_REVIEWS_REQUIRED", "2"))
    require_consensus_for_export: bool = os.getenv("REQUIRE_CONSENSUS_FOR_EXPORT", "1") == "1"
    require_split_metadata: bool = os.getenv("REQUIRE_SPLIT_METADATA", "0") == "1"
    label_codebook_version: str = os.getenv("LABEL_CODEBOOK_VERSION", "real-pilot-label-v1")
    patient_link_ttl_minutes: int = int(os.getenv("PATIENT_LINK_TTL_MINUTES", "4320"))
    question_expiry_minutes: int = int(os.getenv("QUESTION_EXPIRY_MINUTES", "4320"))
    async_worker_poll_seconds: int = int(os.getenv("ASYNC_WORKER_POLL_SECONDS", "5"))
    # W6: how often the worker expires due questions, in seconds.
    question_expiry_scan_seconds: int = int(os.getenv("QUESTION_EXPIRY_SCAN_SECONDS", "60"))
    # W5: login attempts per IP per minute (single-instance in-memory limiter).
    login_rate_limit_per_minute: int = int(os.getenv("LOGIN_RATE_LIMIT_PER_MINUTE", "10"))


settings = Settings()


def production_config_problems(config: Settings = settings) -> list[str]:
    """Return the list of fatal misconfigurations for a production launch."""
    if config.app_env != "production":
        return []
    problems: list[str] = []
    if config.auth_mode != "supabase":
        problems.append("AUTH_MODE must be 'supabase' in production")
    if config.database_url.startswith("sqlite"):
        problems.append("DATABASE_URL must not be SQLite in production")
    if config.storage_backend != "supabase":
        problems.append("STORAGE_BACKEND must be 'supabase' in production")
    if config.db_auto_create:
        problems.append("DB_AUTO_CREATE must be 0 in production")
    if config.auto_seed_demo:
        problems.append("AUTO_SEED_DEMO must be 0 in production")
    if config.randomization_deterministic_seed is not None:
        problems.append("RANDOMIZATION_DETERMINISTIC_SEED must be unset in production")
    origins = cors_origin_list(config)
    if "*" in origins or any("localhost" in origin or "127.0.0.1" in origin for origin in origins):
        problems.append("CORS_ORIGINS must not contain '*' or localhost in production")
    if not config.supabase_secret_key:
        problems.append("SUPABASE_SECRET_KEY must be set in production")
    if not config.supabase_url:
        problems.append("SUPABASE_URL must be set in production")
    if not config.supabase_publishable_key:
        problems.append("SUPABASE_PUBLISHABLE_KEY must be set in production")
    return problems


def validate_production_settings(config: Settings = settings) -> None:
    """Fail fast instead of silently serving an unsafe production config.

    The database ``current_schema()`` probe is deliberately separate (see
    ``backend.db.check_database_schema``) so this function stays side-effect
    free and unit-testable without a database.
    """
    problems = production_config_problems(config)
    if problems:
        raise RuntimeError(
            "refusing to start: unsafe production configuration: " + "; ".join(problems)
        )


def block_size_options() -> tuple[int, ...]:
    values = []
    for item in settings.randomization_block_sizes.split(","):
        item = item.strip()
        if not item:
            continue
        size = int(item)
        if size < len(CHANNELS_MIN) or size % len(CHANNELS_MIN) != 0:
            raise ValueError(f"block size must be a positive multiple of {len(CHANNELS_MIN)}: {size}")
        values.append(size)
    return tuple(values) or (len(CHANNELS_MIN),)


CHANNELS_MIN = ("Structured", "LLM", "Human")


def cors_origin_list(config: Settings = settings) -> list[str]:
    value = config.cors_origins.strip()
    if value == "*":
        return ["*"]
    return [item.strip() for item in value.split(",") if item.strip()]


# Local development needs the export directory to exist (exports / workbooks).
# In production files go to Supabase Storage, so the container filesystem is
# never required to persist anything.
if settings.storage_backend == "local":
    settings.export_dir.mkdir(parents=True, exist_ok=True)
