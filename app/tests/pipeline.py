"""Run the ingestion pipeline synchronously in tests."""

from sqlalchemy import select, update

from app.infrastructure.queue.local import LocalWorker
from app.infrastructure.queue.models import JobStatus, QueueJob
from app.workers import load_tasks


def drain(app, max_jobs: int = 500, *, include_delayed: bool = True) -> int:
    """Run jobs until none are due. Returns how many ran.

    With include_delayed, retry back-offs are skipped so failures surface quickly.
    """
    load_tasks()
    session_factory = app.state.session_factory
    worker = LocalWorker(session_factory, lease_seconds=60, poll_interval_seconds=0, worker_id="test")
    ran = 0
    while ran < max_jobs:
        if include_delayed:
            with session_factory() as session, session.begin():
                session.execute(
                    update(QueueJob)
                    .where(QueueJob.status == JobStatus.QUEUED, QueueJob.task_name.in_(_known()))
                    .values(run_after=QueueJob.created_at)
                )
        if not worker.run_once():
            return ran
        ran += 1
    raise AssertionError("Pipeline did not settle")


def _known() -> list[str]:
    from app.infrastructure.queue.registry import _handlers

    return list(_handlers)


def jobs(app, task_name: str | None = None) -> list[QueueJob]:
    with app.state.session_factory() as session:
        query = select(QueueJob)
        if task_name:
            query = query.where(QueueJob.task_name == task_name)
        return list(session.scalars(query))
