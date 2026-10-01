import secrets
import warnings
from datetime import date
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import PrivateAttr, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnv(StrEnum):
    LOCAL = "local"
    TEST = "test"
    PRODUCTION = "production"


class StorageBackend(StrEnum):
    LOCAL = "local"
    S3 = "s3"


class QueueBackend(StrEnum):
    LOCAL = "local"
    CELERY = "celery"


class WorkerMode(StrEnum):
    IN_PROCESS = "in_process"  # worker thread inside the API process
    DISTRIBUTED = "distributed"  # separate worker processes (python -m app.workers)
    DISABLED = "disabled"  # enqueue only; nothing consumes jobs (tests)


class CacheBackend(StrEnum):
    MEMORY = "memory"
    REDIS = "redis"


class OCRBackend(StrEnum):
    LOCAL = "local"
    WORKER = "worker"
    CLOUD = "cloud"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_ENV: AppEnv = AppEnv.LOCAL
    APP_NAME: str = "Governix"
    APP_VERSION: str = "1.0.0"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = False

    DATABASE_URL: str
    DB_POOL_SIZE: int = 12
    # Overflow connections are opened on demand and closed when idle; they cover
    # burst concurrency without permanently holding connections open. Set to at
    # least half of DB_POOL_SIZE so that a brief traffic spike never starves.
    DB_MAX_OVERFLOW: int = 8
    DB_ECHO: bool = False

    STORAGE_BACKEND: StorageBackend = StorageBackend.LOCAL
    LOCAL_STORAGE_PATH: Path = Path("./storage")
    S3_BUCKET: str | None = None
    S3_ENDPOINT: str | None = None
    S3_REGION: str | None = None
    S3_ACCESS_KEY: SecretStr | None = None
    S3_SECRET_KEY: SecretStr | None = None

    QUEUE_BACKEND: QueueBackend = QueueBackend.LOCAL
    WORKER_MODE: WorkerMode = WorkerMode.IN_PROCESS
    WORKER_CONCURRENCY: int = 1
    WORKER_POLL_INTERVAL_SECONDS: float = 1.0
    JOB_LEASE_SECONDS: int = 300
    JOB_DEFAULT_MAX_ATTEMPTS: int = 3
    REDIS_URL: str | None = None

    # MEMORY is process-local: it works for a single uvicorn worker and for tests,
    # and it is barely a cache once you run more than one worker (each holds its
    # own copy, so the hit rate collapses towards 1/N). A multi-worker or
    # multi-replica deployment must set CACHE_BACKEND=redis; the validator below
    # enforces that instead of leaving it as a silent performance trap.
    CACHE_BACKEND: CacheBackend = CacheBackend.MEMORY
    CACHE_DEFAULT_TTL_SECONDS: int = 300
    CACHE_MAX_ENTRIES: int = 10_000

    OCR_BACKEND: OCRBackend = OCRBackend.LOCAL
    # Vendor-neutral cloud OCR gateway. The endpoint receives a JSON request
    # containing a base64 PNG and returns {"text": "..."}; it can front
    # Textract, Document AI, Azure Document Intelligence, or an internal API.
    CLOUD_OCR_URL: str | None = None
    CLOUD_OCR_API_KEY: SecretStr | None = None
    CLOUD_OCR_TIMEOUT_SECONDS: float = 60.0

    MAX_UPLOAD_SIZE_MB: int = 1024
    # Pages per extraction job; ranges run in parallel on as many workers as exist.
    EXTRACTION_BATCH_PAGES: int = 500
    # A page with fewer extracted characters than this is routed to OCR.
    OCR_MIN_CHARS: int = 25
    OCR_LANGUAGE: str = "eng"
    OCR_DPI: int = 300
    # Numeric date order in documents (01/07/2026): DMY (India/UK) or MDY (US).
    DATE_ORDER: str = "DMY"

    # Policy matching thresholds (see ingestion/analysis/matching.py).
    MATCH_HIGH_CONFIDENCE: float = 0.75
    MATCH_LOW_CONFIDENCE: float = 0.45
    # SimHash Hamming distance at or below which two documents are near-duplicates.
    SIMHASH_DUPLICATE_DISTANCE: int = 3

    # Auth. JWT_SECRET is mandatory in production; locally an ephemeral one is
    # generated (tokens then stop working when the process restarts).
    JWT_SECRET: SecretStr | None = None
    _jwt_secret_is_ephemeral: bool = PrivateAttr(default=False)
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_TTL_MINUTES: int = 15
    REFRESH_TOKEN_TTL_DAYS: int = 7
    LOGIN_MAX_FAILED_ATTEMPTS: int = 5
    LOGIN_LOCKOUT_MINUTES: int = 15
    CORS_ORIGINS: list[str] = ["http://localhost:5173"]

    # AI providers. "openai" is the configured default; "local" providers are
    # deterministic stand-ins for development/tests without an API key.
    LLM_PROVIDER: str = "openai"
    LLM_MODEL: str = "gpt-4o-mini"
    # Per-call bound on the model. A request thread blocked in a synchronous
    # provider call cannot be interrupted from outside, so this -- not the RAG
    # deadline -- is what actually caps latency.
    #
    # Worst case for one answer is roughly
    #     LLM_TIMEOUT_SECONDS * (1 + LLM_TRUNCATION_RETRIES)
    # because a reasoning model that exhausts its token budget is retried once
    # with a doubled budget. At the defaults below that is ~45s, which is what
    # the slowest observed answer actually needs; lowering it trades answer
    # quality on multi-part questions for latency.
    LLM_TIMEOUT_SECONDS: float = 30.0
    LLM_TRUNCATION_RETRIES: int = 1
    # Includes a reasoning model's hidden reasoning tokens (gpt-5 family), not only the answer.
    LLM_MAX_OUTPUT_TOKENS: int = 8000
    EMBEDDING_PROVIDER: str = "openai"
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    # Must match the vector column (see app/modules/search/model.py); changing it needs a migration + re-embed.
    EMBEDDING_DIMENSIONS: int = 1536
    EMBED_BATCH_SIZE: int = 128
    RERANKER_PROVIDER: str = "local"  # local | api | none
    RERANKER_API_URL: str | None = None  # Cohere/Jina-compatible /rerank endpoint
    RERANKER_API_KEY: SecretStr | None = None
    RERANKER_MODEL: str | None = None
    # Reasoning effort for reasoning models (gpt-5 family, o-series); ignored by others.
    # Measured on the acceptance suite: "low" kept accuracy (14/16, same as the
    # model default) while cutting p50 latency 12.2s -> 5.0s and the slowest
    # answer 63s -> 20s. "minimal" is faster again (p50 3.3s) with less headroom
    # for multi-hop questions. None uses the provider default.
    LLM_REASONING_EFFORT: str | None = "low"

    # RAG evidence gate and sizes.
    # Recall budget. RAG_EVIDENCE_LIMIT is the number of passages handed to the
    # model: raising it lifts recall on multi-fact questions at the cost of
    # tokens and of the validator having to clear more claims. Raise it before
    # lowering RAG_MIN_TERM_COVERAGE, which only suppresses the no-answer gate.
    RAG_RETRIEVAL_CANDIDATES: int = 80
    RAG_RERANK_TOP_N: int = 24
    RAG_EVIDENCE_LIMIT: int = 14
    RAG_MIN_EVIDENCE_SCORE: float = 0.18
    RAG_MIN_TERM_COVERAGE: float = 0.4
    # The same, weighted by how rare each term is in what the caller can see:
    # the evidence must cover most of what distinguishes the question.
    RAG_MIN_SALIENT_COVERAGE: float = 0.5
    RAG_CACHE_TTL_SECONDS: int = 600
    # Latest version first; when it has no supported answer, search up to this
    # many earlier versions of each policy, most recent first.
    RAG_PREVIOUS_VERSION_FALLBACK: bool = True
    # AI summary of each version, generated in the background once it is searchable.
    AI_SUMMARY_ENABLED: bool = True
    RAG_FALLBACK_MAX_DEPTH: int = 3
    # Per-principal token-bucket rate limit on /ai/ask, which is the only route
    # to a paid LLM. 0 disables the limiter (tests, single-user local runs).
    RAG_RATE_LIMIT_REQUESTS: int = 20
    RAG_RATE_LIMIT_WINDOW_SECONDS: int = 60
    RAG_RATE_LIMIT_ENABLED: bool = True
    # Wall-clock budget for a whole answer, checked before the LLM call. This
    # covers retrieval and evidence building only; the call itself is bounded by
    # LLM_TIMEOUT_SECONDS, which must be the smaller of the two.
    RAG_DEADLINE_SECONDS: float = 20.0
    OPENAI_API_KEY: SecretStr | None = None
    OPENAI_BASE_URL: str | None = None
    # Credit view on the dashboard. OpenAI does not let an API key read its balance, so
    # the credit added (and from when) is stated here; spend is subtracted from it.
    OPENAI_CREDIT_USD: float | None = None
    OPENAI_CREDIT_SINCE: date | None = None
    # Optional organization Admin key (sk-admin-...): exact spend from OpenAI's Costs API
    # instead of an estimate from recorded tokens.
    OPENAI_ADMIN_KEY: SecretStr | None = None
    # Price overrides, USD per 1M tokens: {"gpt-4o-mini": [0.15, 0.60]} (input, output).
    OPENAI_PRICES: dict[str, list[float]] = {}

    @field_validator("DATABASE_URL")
    @classmethod
    def use_psycopg3_driver(cls, value: str) -> str:
        # Accept plain postgresql:// or legacy psycopg2 URLs; always run on psycopg 3.
        for prefix in ("postgresql+psycopg2://", "postgresql://", "postgres://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix):]
        return value

    @model_validator(mode="after")
    def check_backend_requirements(self) -> "Settings":
        # The RAG limiter is per-process (app/core/rate_limit.py), so it bounds
        # the per-worker rate. Refuse to pretend otherwise in production.
        if self.APP_ENV is AppEnv.PRODUCTION and not self.RAG_RATE_LIMIT_ENABLED:
            raise ValueError(
                "RAG_RATE_LIMIT_ENABLED=false is not allowed in production: /ai/ask spends LLM budget."
            )
        if self.APP_ENV is AppEnv.PRODUCTION and self.RAG_DEADLINE_SECONDS <= 0:
            raise ValueError("RAG_DEADLINE_SECONDS must be positive in production.")
        if self.RAG_DEADLINE_SECONDS >= self.LLM_TIMEOUT_SECONDS:
            raise ValueError(
                f"RAG_DEADLINE_SECONDS ({self.RAG_DEADLINE_SECONDS}) must be below "
                f"LLM_TIMEOUT_SECONDS ({self.LLM_TIMEOUT_SECONDS}): the deadline is only "
                "checked before the model is called, so it cannot bound the call itself."
            )
        # DB pool sizing: the hybrid retrieval opens up to LANE_POOL_SIZE (8) connections
        # in parallel. With 4 Uvicorn workers each handling 2 concurrent RAG requests,
        # peak demand is 4 × 2 × 4 lanes = 32 connections. Warn (not error) so that
        # under-provisioned deployments fail visibly at startup rather than under load.
        from app.modules.search.retrieval import LANE_POOL_SIZE
        min_pool = LANE_POOL_SIZE + 4  # conservative: 1 worker, 1 concurrent request
        if self.DB_POOL_SIZE + self.DB_MAX_OVERFLOW < min_pool:
            warnings.warn(
                f"DB_POOL_SIZE ({self.DB_POOL_SIZE}) + DB_MAX_OVERFLOW ({self.DB_MAX_OVERFLOW}) = "
                f"{self.DB_POOL_SIZE + self.DB_MAX_OVERFLOW} may be too small: the hybrid retrieval "
                f"opens up to {LANE_POOL_SIZE} parallel connections per request. "
                f"Set DB_POOL_SIZE >= {min_pool} to avoid pool starvation.",
                RuntimeWarning,
                stacklevel=2,
            )
        missing: list[str] = []
        if self.STORAGE_BACKEND is StorageBackend.S3:
            missing += [
                name
                for name in ("S3_BUCKET", "S3_ACCESS_KEY", "S3_SECRET_KEY")
                if getattr(self, name) is None
            ]
        if (
            self.QUEUE_BACKEND is QueueBackend.CELERY
            or self.CACHE_BACKEND is CacheBackend.REDIS
        ) and not self.REDIS_URL:
            missing.append("REDIS_URL")
        if missing:
            raise ValueError(f"Missing required settings: {', '.join(sorted(set(missing)))}")

        if (
            self.QUEUE_BACKEND is QueueBackend.CELERY
            and self.WORKER_MODE is WorkerMode.IN_PROCESS
        ):
            raise ValueError("WORKER_MODE=in_process is only supported with QUEUE_BACKEND=local")

        if self.OCR_BACKEND is OCRBackend.CLOUD:
            missing += [name for name in ("CLOUD_OCR_URL", "CLOUD_OCR_API_KEY") if getattr(self, name) is None]
        if missing:
            raise ValueError(f"Missing required settings: {', '.join(sorted(set(missing)))}")

            # A process-local cache cannot be shared between workers, so a
        # production deployment must use Redis to keep the hit rate.
        if (
            self.APP_ENV is AppEnv.PRODUCTION
            and self.CACHE_BACKEND is CacheBackend.MEMORY
        ):
            warnings.warn(
                "CACHE_BACKEND=memory in production: the cache is per-process, so every "
                "worker keeps its own copy and the hit rate collapses. Set "
                "CACHE_BACKEND=redis and REDIS_URL to share it.",
                RuntimeWarning,
                stacklevel=2,
            )

        if self.JWT_SECRET is None:
            if self.APP_ENV is AppEnv.PRODUCTION:
                raise ValueError("Missing required settings: JWT_SECRET")
            self.JWT_SECRET = SecretStr(secrets.token_urlsafe(48))
            self._jwt_secret_is_ephemeral = True
        elif len(self.JWT_SECRET.get_secret_value()) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters.")
        return self

    @property
    def jwt_secret_is_ephemeral(self) -> bool:
        return self._jwt_secret_is_ephemeral


@lru_cache
def get_settings() -> Settings:
    return Settings()
