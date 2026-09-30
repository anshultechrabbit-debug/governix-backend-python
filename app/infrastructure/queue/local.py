import logging
import os
import socket
import threading
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.logging import redact
from app.infrastructure.queue.base import Queue
from app.infrastructure.queue.models import JobStatus, QueueJob
from app.infrastructure.queue.registry import JobContext, get_handler

logger = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 2000
RETRY_BASE_SECONDS = 5
RETRY_MAX_SECONDS = 300


class LocalQueue(Queue):
    """Postgres-backed queue: jobs survive restarts without Redis or Celery."""

    def __init__(self, session_factory: sessionmaker[Session], default_max_attempts: int) -> None:
        self.session_factory = session_factory
        self.default_max_attempts = default_max_attempts

    def enqueue(
        self,
        task_name: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        organization_id: uuid.UUID | None = None,
        priority: int = 0,
        max_attempts: int | None = None,
        delay_seconds: float = 0,
        session: Session | None = None,
    ) -> uuid.UUID:
        values = {
            "id": uuid.uuid4(),
            "task_name": task_name,
            "payload": payload,
            "status": JobStatus.QUEUED,
            "priority": priority,
            "attempts": 0,
            "max_attempts": max_attempts or self.default_max_attempts,
            "idempotency_key": idempotency_key,
            "organization_id": organization_id,
            "run_after": func.now() + timedelta(seconds=delay_seconds),
        }
        if session is not None:
            # Same transaction as the caller's business change (transactional outbox).
            return self._insert(session, values, idempotency_key)
        with self.session_factory() as own_session, own_session.begin():
            return self._insert(own_session, values, idempotency_key)

    @staticmethod
    def _insert(session: Session, values: dict[str, Any], idempotency_key: str | None) -> uuid.UUID:
        job_id = session.execute(
            insert(QueueJob)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(QueueJob.id)
        ).scalar_one_or_none()
        if job_id is None:
            job_id = session.execute(
                select(QueueJob.id).where(QueueJob.idempotency_key == idempotency_key)
            ).scalar_one()
        return job_id


class LocalWorker:
    """Claims and runs jobs from the Postgres queue.

    Claims use FOR UPDATE SKIP LOCKED, so any number of workers (threads or
    processes) can poll the same table without double-claiming. A claimed job
    holds a lease; if the worker dies, the lease expires and the job is retried.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        lease_seconds: int,
        poll_interval_seconds: float,
        worker_id: str | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.lease_seconds = lease_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

    def recover_expired_leases(self) -> int:
        """Requeue (or fail, if out of attempts) jobs whose worker stopped heartbeating."""
        expired = QueueJob.locked_at < func.now() - timedelta(seconds=self.lease_seconds)
        with self.session_factory() as session, session.begin():
            failed = session.execute(
                update(QueueJob)
                .where(QueueJob.status == JobStatus.RUNNING, expired)
                .where(QueueJob.attempts >= QueueJob.max_attempts)
                .values(
                    status=JobStatus.FAILED,
                    finished_at=func.now(),
                    locked_at=None,
                    locked_by=None,
                    last_error="Lease expired: worker stopped responding.",
                )
            ).rowcount
            requeued = session.execute(
                update(QueueJob)
                .where(QueueJob.status == JobStatus.RUNNING, expired)
                .values(
                    status=JobStatus.QUEUED,
                    run_after=func.now(),
                    locked_at=None,
                    locked_by=None,
                    last_error="Lease expired: worker stopped responding.",
                )
            ).rowcount
        if failed or requeued:
            logger.warning("Recovered expired job leases: requeued=%s failed=%s", requeued, failed)
        return failed + requeued

    def run_once(self) -> bool:
        """Claim and run at most one job. Returns False if nothing was due."""
        claimed = self._claim()
        if claimed is None:
            return False
        self._run_claimed(claimed)
        return True

    def run_job(self, job_id: uuid.UUID) -> bool:
        """Run one named durable job (used by the Celery transport)."""
        claimed = self._claim(job_id)
        if claimed is None:
            return False
        self._run_claimed(claimed)
        return True

    def _run_claimed(self, claimed) -> None:
        job_id, task_name, payload, attempt, max_attempts, organization_id = claimed

        context = JobContext(
            job_id=job_id,
            attempt=attempt,
            max_attempts=max_attempts,
            organization_id=organization_id,
            heartbeat=lambda: self._heartbeat(job_id),
        )
        handler = get_handler(task_name)
        try:
            if handler is None:
                # Retryable: during a rolling deploy an older worker may not know a new task yet.
                raise LookupError(f"No handler registered for task {task_name!r}")
            handler(payload, context)
        except Exception as exc:
            self._record_failure(job_id, attempt, max_attempts, exc)
        else:
            self._finish(job_id)

    def run_forever(self, stop_event: threading.Event) -> None:
        logger.info("Worker %s started", self.worker_id)
        polls = 0
        while not stop_event.is_set():
            try:
                if polls % 30 == 0:
                    self.recover_expired_leases()
                polls += 1
                if not self.run_once():
                    stop_event.wait(self.poll_interval_seconds)
            except Exception:
                # Database blips must not kill the worker loop.
                logger.exception("Worker %s loop error", self.worker_id)
                stop_event.wait(self.poll_interval_seconds)
        logger.info("Worker %s stopped", self.worker_id)

    def _claim(self, job_id: uuid.UUID | None = None):
        next_job = (
            select(QueueJob.id)
            .where(QueueJob.status == JobStatus.QUEUED, QueueJob.run_after <= func.now())
            .order_by(QueueJob.priority.desc(), QueueJob.run_after)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        if job_id is not None:
            next_job = (
                select(QueueJob.id)
                .where(QueueJob.id == job_id, QueueJob.status == JobStatus.QUEUED, QueueJob.run_after <= func.now())
                .with_for_update(skip_locked=True)
                .scalar_subquery()
            )
        with self.session_factory() as session, session.begin():
            return session.execute(
                update(QueueJob)
                .where(QueueJob.id == next_job)
                .values(
                    status=JobStatus.RUNNING,
                    attempts=QueueJob.attempts + 1,
                    locked_at=func.now(),
                    locked_by=self.worker_id,
                )
                .returning(
                    QueueJob.id,
                    QueueJob.task_name,
                    QueueJob.payload,
                    QueueJob.attempts,
                    QueueJob.max_attempts,
                    QueueJob.organization_id,
                )
            ).one_or_none()

    def _owned(self, job_id: uuid.UUID):
        # Guards every state change: if our lease expired and another worker
        # took the job over, this worker must not overwrite its state.
        return (
            QueueJob.id == job_id,
            QueueJob.status == JobStatus.RUNNING,
            QueueJob.locked_by == self.worker_id,
        )

    def _heartbeat(self, job_id: uuid.UUID) -> None:
        with self.session_factory() as session, session.begin():
            session.execute(update(QueueJob).where(*self._owned(job_id)).values(locked_at=func.now()))

    def _finish(self, job_id: uuid.UUID) -> None:
        with self.session_factory() as session, session.begin():
            session.execute(
                update(QueueJob)
                .where(*self._owned(job_id))
                .values(
                    status=JobStatus.SUCCEEDED,
                    finished_at=func.now(),
                    locked_at=None,
                    locked_by=None,
                    last_error=None,
                )
            )

    def _record_failure(
        self, job_id: uuid.UUID, attempt: int, max_attempts: int, exc: Exception
    ) -> None:
        error = redact(f"{type(exc).__name__}: {exc}")[:MAX_ERROR_LENGTH]
        if attempt >= max_attempts:
            logger.error("Job %s failed permanently after %s attempts: %s", job_id, attempt, error)
            values: dict[str, Any] = {"status": JobStatus.FAILED, "finished_at": func.now()}
        else:
            backoff = min(RETRY_BASE_SECONDS * 2 ** (attempt - 1), RETRY_MAX_SECONDS)
            logger.warning("Job %s attempt %s failed, retrying in %ss: %s", job_id, attempt, backoff, error)
            values = {
                "status": JobStatus.QUEUED,
                "run_after": func.now() + timedelta(seconds=backoff),
            }
        with self.session_factory() as session, session.begin():
            session.execute(
                update(QueueJob)
                .where(*self._owned(job_id))
                .values(**values, locked_at=None, locked_by=None, last_error=error)
            )
