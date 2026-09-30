"""Celery transport for the durable PostgreSQL job outbox.

Celery delivers work; QueueJob remains the source of truth for idempotency,
leases, retries and auditability. This keeps local and production semantics
identical instead of creating a second, subtly different pipeline.
"""

import uuid
from typing import Any

from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.infrastructure.queue.local import LocalQueue


class CeleryQueue(LocalQueue):
    """Transactional outbox which dispatches committed jobs to Celery."""

    def __init__(self, settings: Settings, session_factory: sessionmaker[Session]) -> None:
        try:
            from celery import Celery
        except ImportError as exc:  # pragma: no cover - deployment extra
            raise RuntimeError("QUEUE_BACKEND=celery requires the production dependencies.") from exc
        super().__init__(session_factory, default_max_attempts=settings.JOB_DEFAULT_MAX_ATTEMPTS)
        self.celery = Celery("governix", broker=settings.REDIS_URL)

    def enqueue(
        self, task_name: str, payload: dict[str, Any], *, idempotency_key: str | None = None,
        organization_id: uuid.UUID | None = None, priority: int = 0, max_attempts: int | None = None,
        delay_seconds: float = 0, session: Session | None = None,
    ) -> uuid.UUID:
        job_id = super().enqueue(
            task_name, payload, idempotency_key=idempotency_key, organization_id=organization_id,
            priority=priority, max_attempts=max_attempts, delay_seconds=delay_seconds, session=session,
        )
        if session is None:
            self._dispatch(job_id, delay_seconds)
        else:
            pending = session.info.setdefault("governix_celery_jobs", [])
            if not pending:
                event.listen(session, "after_commit", self._dispatch_committed, once=True)
                event.listen(session, "after_rollback", self._discard_rolled_back, once=True)
            pending.append((job_id, delay_seconds))
        return job_id

    def _dispatch_committed(self, session: Session) -> None:
        for job_id, delay in session.info.pop("governix_celery_jobs", []):
            self._dispatch(job_id, delay)

    @staticmethod
    def _discard_rolled_back(session: Session) -> None:
        session.info.pop("governix_celery_jobs", None)

    def _dispatch(self, job_id: uuid.UUID, delay_seconds: float) -> None:
        self.celery.send_task("governix.execute_job", args=[str(job_id)], countdown=max(0, delay_seconds))
