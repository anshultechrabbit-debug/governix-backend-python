"""Standalone worker process for the local (Postgres) queue.

    python -m app.workers

Run as many as needed (WORKER_MODE=distributed); SKIP LOCKED prevents double-claims.
"""

import signal
import threading

from app.core.config import QueueBackend, get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.logging import configure_logging
from app.infrastructure.cache.factory import create_cache
from app.infrastructure.queue.factory import create_queue
from app.infrastructure.queue.local import LocalWorker
from app.infrastructure.storage.factory import create_storage
from app.workers import load_tasks
from app.workers.runtime import Runtime, configure_runtime


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    if settings.QUEUE_BACKEND is not QueueBackend.LOCAL:
        raise SystemExit("For QUEUE_BACKEND=celery run: celery -A app.workers.celery_app worker")

    load_tasks()
    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())

    engine = create_db_engine(settings)
    session_factory = create_session_factory(engine)
    queue = create_queue(settings, session_factory)
    configure_runtime(Runtime(
        settings=settings,
        session_factory=session_factory,
        storage=create_storage(settings),
        queue=queue,
        cache=create_cache(settings),
    ))
    # Documents that failed only because a provider was missing (e.g. no API
    # key) resume by themselves once the worker starts with it configured.
    from app.modules.ingestion.recovery import startup_sweep

    startup_sweep(session_factory)
    from app.workers.platform.tasks import schedule_daily

    schedule_daily(queue)

    worker = LocalWorker(
        session_factory,
        lease_seconds=settings.JOB_LEASE_SECONDS,
        poll_interval_seconds=settings.WORKER_POLL_INTERVAL_SECONDS,
    )
    try:
        worker.run_forever(stop_event)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
