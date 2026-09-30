from sqlalchemy.orm import Session, sessionmaker

from app.core.config import QueueBackend, Settings
from app.infrastructure.queue.base import Queue


def create_queue(settings: Settings, session_factory: sessionmaker[Session]) -> Queue:
    if settings.QUEUE_BACKEND is QueueBackend.CELERY:
        from app.infrastructure.queue.celery import CeleryQueue

        return CeleryQueue(settings, session_factory)

    from app.infrastructure.queue.local import LocalQueue

    return LocalQueue(session_factory, default_max_attempts=settings.JOB_DEFAULT_MAX_ATTEMPTS)
