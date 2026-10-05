import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class JobContext:
    job_id: uuid.UUID
    attempt: int
    max_attempts: int
    organization_id: uuid.UUID | None
    # Extends the job's lease. Long-running handlers (e.g. a 2,000-page range)
    # must call this periodically, well within JOB_LEASE_SECONDS.
    heartbeat: Callable[[], None]

    @property
    def is_final_attempt(self) -> bool:
        return self.attempt >= self.max_attempts


class RetryLater(Exception):
    """Run the job again after `delay_seconds` without spending an attempt.

    For waits that are expected to pass, such as a provider rate limit: many jobs
    hitting the limit at once must queue up behind it, not fail after max_attempts.
    """

    def __init__(self, delay_seconds: float, reason: str) -> None:
        super().__init__(reason)
        self.delay_seconds = delay_seconds


TaskHandler = Callable[[dict[str, Any], JobContext], None]

_handlers: dict[str, TaskHandler] = {}


def task(name: str) -> Callable[[TaskHandler], TaskHandler]:
    """Register a function as the handler for `name`."""

    def decorator(func: TaskHandler) -> TaskHandler:
        if name in _handlers and _handlers[name] is not func:
            raise ValueError(f"Task {name!r} is already registered.")
        _handlers[name] = func
        return func

    return decorator


def get_handler(name: str) -> TaskHandler | None:
    return _handlers.get(name)


def unregister(name: str) -> None:
    _handlers.pop(name, None)
