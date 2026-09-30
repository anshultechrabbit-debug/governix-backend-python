"""Celery worker entrypoint: ``celery -A app.workers.celery_app worker``."""

import uuid
from datetime import UTC, datetime
from functools import lru_cache

from celery import Celery
from celery.signals import worker_ready
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.logging import configure_logging
from app.infrastructure.cache.factory import create_cache
from app.infrastructure.queue.local import LocalWorker
from app.infrastructure.queue.models import JobStatus, QueueJob
from app.infrastructure.storage.factory import create_storage
from app.workers import load_tasks
from app.workers.runtime import Runtime, configure_runtime

settings = get_settings()
celery = Celery("governix", broker=settings.REDIS_URL)
celery.conf.update(task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1)


@lru_cache(maxsize=1)
def _runtime():
    configure_logging(settings)
    load_tasks()
    engine = create_db_engine(settings)
    factory = create_session_factory(engine)
    from app.infrastructure.queue.factory import create_queue
    configure_runtime(Runtime(settings, factory, create_storage(settings), create_queue(settings, factory), create_cache(settings)))
    return factory


@celery.task(bind=True, name="governix.execute_job")
def execute_job(self, job_id: str):
    factory = _runtime()
    worker = LocalWorker(factory, lease_seconds=settings.JOB_LEASE_SECONDS, poll_interval_seconds=0)
    worker.run_job(uuid.UUID(job_id))
    # LocalWorker records a failed attempt as QUEUED with exponential run_after.
    # Re-deliver the same durable job at that time; successful jobs are terminal.
    with factory() as session:
        job = session.scalar(select(QueueJob).where(QueueJob.id == uuid.UUID(job_id)))
        if job and job.status == JobStatus.QUEUED:
            seconds = max(1, int((job.run_after - datetime.now(UTC)).total_seconds()))
            raise self.retry(countdown=seconds)


@worker_ready.connect
def resume_after_provider_recovery(**_kwargs) -> None:
    """Resume documents that failed only because a provider was missing (see ingestion.recovery)."""
    from app.modules.ingestion.recovery import startup_sweep

    startup_sweep(_runtime())
