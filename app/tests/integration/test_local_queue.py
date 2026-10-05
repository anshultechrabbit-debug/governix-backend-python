import threading
import uuid

import pytest
from sqlalchemy import select, text, update

from app.core.database import create_session_factory
from app.infrastructure.queue import registry
from app.infrastructure.queue.local import LocalQueue, LocalWorker
from app.infrastructure.queue.models import JobStatus, QueueJob

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(db_engine):
    return create_session_factory(db_engine)


@pytest.fixture
def queue(session_factory):
    return LocalQueue(session_factory, default_max_attempts=3)


def make_worker(session_factory, name="w1", lease_seconds=60):
    return LocalWorker(
        session_factory, lease_seconds=lease_seconds, poll_interval_seconds=0.01, worker_id=name
    )


@pytest.fixture
def task_name():
    name = f"test.{uuid.uuid4().hex}"
    yield name
    registry.unregister(name)


def load(session_factory, job_id) -> QueueJob:
    with session_factory() as session:
        return session.get(QueueJob, job_id)


def make_due(session_factory, job_id):
    with session_factory() as session, session.begin():
        session.execute(
            update(QueueJob).where(QueueJob.id == job_id).values(run_after=text("now()"))
        )


def test_enqueue_is_idempotent(queue, session_factory):
    first = queue.enqueue("t", {"a": 1}, idempotency_key="doc-1:extract")
    second = queue.enqueue("t", {"a": 2}, idempotency_key="doc-1:extract")
    assert first == second
    with session_factory() as session:
        assert session.scalars(select(QueueJob)).all()[0].payload == {"a": 1}


def test_successful_job(queue, session_factory, task_name):
    seen = []
    registry.task(task_name)(lambda payload, ctx: seen.append((payload, ctx.attempt)))
    org = uuid.uuid4()
    job_id = queue.enqueue(task_name, {"doc": "d1"}, organization_id=org)

    assert make_worker(session_factory).run_once() is True
    assert seen == [({"doc": "d1"}, 1)]
    job = load(session_factory, job_id)
    assert job.status == JobStatus.SUCCEEDED
    assert job.organization_id == org
    assert job.locked_by is None and job.finished_at is not None
    assert make_worker(session_factory).run_once() is False


def test_delayed_job_is_not_claimed_early(queue, session_factory, task_name):
    registry.task(task_name)(lambda payload, ctx: None)
    queue.enqueue(task_name, {}, delay_seconds=3600)
    assert make_worker(session_factory).run_once() is False


def test_priority_order(queue, session_factory, task_name):
    order = []
    registry.task(task_name)(lambda payload, ctx: order.append(payload["n"]))
    queue.enqueue(task_name, {"n": "low"})
    queue.enqueue(task_name, {"n": "high"}, priority=10)
    worker = make_worker(session_factory)
    worker.run_once()
    worker.run_once()
    assert order == ["high", "low"]


def test_retries_with_backoff_then_fails(queue, session_factory, task_name):
    def boom(payload, ctx):
        raise RuntimeError("extraction failed password=hunter2")

    registry.task(task_name)(boom)
    job_id = queue.enqueue(task_name, {}, max_attempts=2)
    worker = make_worker(session_factory)

    worker.run_once()
    job = load(session_factory, job_id)
    assert job.status == JobStatus.QUEUED and job.attempts == 1
    assert "hunter2" not in job.last_error
    assert worker.run_once() is False  # backoff: not due yet

    make_due(session_factory, job_id)
    worker.run_once()
    job = load(session_factory, job_id)
    assert job.status == JobStatus.FAILED and job.attempts == 2


def test_retry_later_waits_without_spending_attempts(queue, session_factory, task_name):
    calls = []

    def rate_limited(payload, ctx):
        calls.append(ctx.attempt)
        if len(calls) <= 3:
            raise registry.RetryLater(3600, "rate limit")

    registry.task(task_name)(rate_limited)
    job_id = queue.enqueue(task_name, {}, max_attempts=1)
    worker = make_worker(session_factory)

    for _ in range(3):
        make_due(session_factory, job_id)
        worker.run_once()
        job = load(session_factory, job_id)
        assert job.status == JobStatus.QUEUED and job.attempts == 0
        assert worker.run_once() is False  # deferred: not due yet

    make_due(session_factory, job_id)
    worker.run_once()
    assert load(session_factory, job_id).status == JobStatus.SUCCEEDED
    assert calls == [1, 1, 1, 1]


def test_unknown_task_is_retried_not_dropped(queue, session_factory):
    job_id = queue.enqueue("not.registered", {})
    make_worker(session_factory).run_once()
    job = load(session_factory, job_id)
    assert job.status == JobStatus.QUEUED
    assert "No handler registered" in job.last_error


def test_expired_lease_is_recovered_and_stale_worker_cannot_overwrite(
    queue, session_factory, task_name
):
    release = threading.Event()
    registry.task(task_name)(lambda payload, ctx: release.wait(5))
    job_id = queue.enqueue(task_name, {})
    crashed = make_worker(session_factory, "crashed")
    thread = threading.Thread(target=crashed.run_once)
    thread.start()

    # Simulate the lease running out while "crashed" is still stuck in the handler.
    with session_factory() as session, session.begin():
        session.execute(
            update(QueueJob)
            .where(QueueJob.id == job_id)
            .values(locked_at=text("now() - interval '1 hour'"))
        )
    assert make_worker(session_factory, "sweeper").recover_expired_leases() == 1
    assert load(session_factory, job_id).status == JobStatus.QUEUED

    rescuer = make_worker(session_factory, "rescuer")
    registry.unregister(task_name)
    registry.task(task_name)(lambda payload, ctx: None)
    rescuer.run_once()
    assert load(session_factory, job_id).status == JobStatus.SUCCEEDED

    # The stale worker finishing late must not clobber the rescuer's result.
    release.set()
    thread.join()
    job = load(session_factory, job_id)
    assert job.status == JobStatus.SUCCEEDED and job.attempts == 2


def test_heartbeat_extends_lease(queue, session_factory, task_name):
    def long_job(payload, ctx):
        with session_factory() as session, session.begin():
            session.execute(
                update(QueueJob)
                .where(QueueJob.id == ctx.job_id)
                .values(locked_at=text("now() - interval '1 hour'"))
            )
        ctx.heartbeat()
        assert make_worker(session_factory, "sweeper").recover_expired_leases() == 0

    registry.task(task_name)(long_job)
    job_id = queue.enqueue(task_name, {})
    make_worker(session_factory).run_once()
    assert load(session_factory, job_id).status == JobStatus.SUCCEEDED


def test_concurrent_workers_never_double_claim(queue, session_factory, task_name):
    runs: list[str] = []
    lock = threading.Lock()

    def record(payload, ctx):
        with lock:
            runs.append(payload["n"])

    registry.task(task_name)(record)
    for n in range(40):
        queue.enqueue(task_name, {"n": str(n)})

    stop = threading.Event()
    workers = [make_worker(session_factory, f"w{i}") for i in range(4)]
    threads = [threading.Thread(target=w.run_forever, args=(stop,)) for w in workers]
    for t in threads:
        t.start()
    for _ in range(500):
        with session_factory() as session:
            remaining = session.scalar(
                select(QueueJob.id).where(QueueJob.status != JobStatus.SUCCEEDED).limit(1)
            )
        if remaining is None:
            break
        stop.wait(0.02)
    stop.set()
    for t in threads:
        t.join()

    assert sorted(runs, key=int) == [str(n) for n in range(40)]


def test_enqueue_in_callers_transaction_is_rolled_back_with_it(queue, session_factory):
    with session_factory() as session:
        queue.enqueue("t", {}, session=session)
        session.rollback()
    with session_factory() as session, session.begin():
        queue.enqueue("t", {"kept": True}, session=session)
    with session_factory() as session:
        assert [j.payload for j in session.scalars(select(QueueJob))] == [{"kept": True}]
