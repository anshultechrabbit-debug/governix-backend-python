import uuid
from abc import ABC, abstractmethod
from typing import Any

from sqlalchemy.orm import Session


class Queue(ABC):
    """Background job queue.

    Handlers must be idempotent: a job can run more than once (retries, lease
    expiry after a crash). Payloads must be JSON-serialisable (pass UUIDs as str).
    """

    @abstractmethod
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
        """Schedule a job and return its id.

        Enqueueing twice with the same `idempotency_key` returns the existing
        job's id instead of creating a duplicate.

        With `session`, the job is only dispatched if that transaction commits
        (the caller commits), so a job never refers to a row that was rolled back.
        """
