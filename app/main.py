import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import QueueBackend, Settings, WorkerMode, get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.error_handlers import register_error_handlers
from app.core.logging import configure_logging
from app.core.metrics import Metrics, MetricsMiddleware
from app.core.rate_limit import RateLimiter
from app.core.request_context import RequestIdMiddleware
from app.infrastructure.cache.factory import create_cache
from app.infrastructure.queue.factory import create_queue
from app.infrastructure.storage.factory import create_storage
from app.modules.assignments.router import router as assignments_router
from app.modules.audit.router import router as audit_router
from app.modules.auth.router import router as auth_router
from app.modules.branches.router import router as branches_router
from app.modules.categories.router import router as categories_router
from app.modules.dashboard.router import router as dashboard_router
from app.modules.departments.router import router as departments_router
from app.modules.documents.router import router as documents_router
from app.modules.health.router import router as health_router
from app.modules.ingestion.router import router as ingestion_router
from app.modules.notifications.router import router as notifications_router
from app.modules.organizations.router import router as organizations_router
from app.modules.policies.router import router as policies_router
from app.modules.rag.router import router as rag_router
from app.modules.search.retrieval import shutdown_lane_pool
from app.modules.search.router import router as search_router
from app.modules.tickets.router import router as tickets_router
from app.modules.uploads.router import router as uploads_router
from app.modules.users.router import router as users_router
from app.workers.runtime import Runtime, configure_runtime

logger = logging.getLogger(__name__)


def _start_in_process_workers(app: FastAPI, settings: Settings) -> None:
    from app.infrastructure.queue.local import LocalWorker
    from app.workers import load_tasks

    load_tasks()
    stop_event = threading.Event()
    threads = []
    for index in range(settings.WORKER_CONCURRENCY):
        worker = LocalWorker(
            app.state.session_factory,
            lease_seconds=settings.JOB_LEASE_SECONDS,
            poll_interval_seconds=settings.WORKER_POLL_INTERVAL_SECONDS,
        )
        thread = threading.Thread(
            target=worker.run_forever, args=(stop_event,), name=f"governix-worker-{index}", daemon=True
        )
        thread.start()
        threads.append(thread)
    app.state.worker_stop_event = stop_event
    app.state.worker_threads = threads

    from app.modules.ingestion.recovery import startup_sweep
    from app.workers.platform.tasks import schedule_daily

    startup_sweep(app.state.session_factory)
    schedule_daily(app.state.queue)


def _stop_in_process_workers(app: FastAPI) -> None:
    stop_event = getattr(app.state, "worker_stop_event", None)
    if stop_event is None:
        return
    stop_event.set()
    for thread in app.state.worker_threads:
        # A handler mid-job may outlive this; its lease expires and the job is retried.
        thread.join(timeout=10)


def validate_settings(settings: Settings) -> None:
    from app.core.exceptions import ConfigurationError
    from app.modules.search.model import VECTOR_DIMENSIONS

    if settings.EMBEDDING_DIMENSIONS != VECTOR_DIMENSIONS:
        raise ConfigurationError(
            f"EMBEDDING_DIMENSIONS={settings.EMBEDDING_DIMENSIONS} does not match the vector column "
            f"({VECTOR_DIMENSIONS}). Changing it requires a migration and re-embedding all chunks."
        )
    if "openai" in (settings.LLM_PROVIDER, settings.EMBEDDING_PROVIDER) and settings.OPENAI_API_KEY is None:
        logger.warning("OPENAI_API_KEY is not set: OpenAI-backed indexing and answering will fail until it is.")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    validate_settings(settings)
    if settings.jwt_secret_is_ephemeral:
        logger.warning(
            "JWT_SECRET not set: using an ephemeral secret. Sessions end on restart and "
            "multi-process deployments will reject each other's tokens."
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if (
            settings.QUEUE_BACKEND is QueueBackend.LOCAL
            and settings.WORKER_MODE is WorkerMode.IN_PROCESS
        ):
            _start_in_process_workers(app, settings)
        try:
            yield
        finally:
            _stop_in_process_workers(app)
            shutdown_lane_pool()
            engine.dispose()

    app = FastAPI(title=settings.APP_NAME, version=settings.APP_VERSION, lifespan=lifespan)

    engine = create_db_engine(settings)
    app.state.settings = settings
    app.state.metrics = Metrics()
    app.state.engine = engine
    app.state.session_factory = create_session_factory(engine)
    app.state.storage = create_storage(settings)
    app.state.cache = create_cache(settings)
    app.state.queue = create_queue(settings, app.state.session_factory)
    # Bounds the rate at which the paid LLM path can be driven.
    app.state.ai_limiter = RateLimiter(
        settings.RAG_RATE_LIMIT_REQUESTS,
        settings.RAG_RATE_LIMIT_WINDOW_SECONDS,
        enabled=settings.RAG_RATE_LIMIT_ENABLED,
    )
    configure_runtime(Runtime(
        settings=settings,
        session_factory=app.state.session_factory,
        storage=app.state.storage,
        queue=app.state.queue,
        cache=app.state.cache,
    ))

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(MetricsMiddleware, metrics=app.state.metrics)
    register_error_handlers(app)
    for router in (
        health_router,
        auth_router,
        organizations_router,
        branches_router,
        departments_router,
        users_router,
        audit_router,
        categories_router,
        documents_router,
        ingestion_router,
        policies_router,
        search_router,
        rag_router,
        dashboard_router,
        uploads_router,
        assignments_router,
        notifications_router,
        tickets_router,
    ):
        app.include_router(router)
    return app


app = create_app()
