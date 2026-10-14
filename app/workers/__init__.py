import importlib

# Modules whose @task handlers the worker must register (filled from Phase 4 on).
TASK_MODULES: list[str] = [
    "app.workers.extraction.tasks",
    "app.workers.ocr.tasks",
    "app.workers.structure.tasks",
    "app.workers.analysis.tasks",
    "app.workers.chunking.tasks",
    "app.workers.embeddings.tasks",
    "app.workers.validation.tasks",
    "app.workers.platform.tasks",
]


def load_tasks() -> None:
    for module in TASK_MODULES:
        importlib.import_module(module)


def network_bound_tasks() -> frozenset[str]:
    """Jobs that spend most of their time waiting on the AI provider (and its per-minute quota)."""
    from app.modules.ingestion import pipeline
    from app.modules.versions.summary import SUMMARIZE

    return frozenset({pipeline.EMBED_BATCH, pipeline.ANALYZE, SUMMARIZE})


def worker_lanes(session_factory, settings, general: int) -> list:
    """`general` workers for CPU work (extraction, OCR, structure, chunking) and every other job,
    plus WORKER_NETWORK_SLOTS workers for network-bound jobs only.

    With one worker, a bulk upload ran strictly one job at a time: while an embedding batch waited
    for OpenAI or for the tokens-per-minute budget, no other PDF was read. Separate lanes let the
    waits overlap the CPU work. Jobs are safe to run concurrently (SKIP LOCKED claims, idempotent
    steps), as with several worker processes.
    """
    from app.infrastructure.queue.local import LocalWorker

    network = network_bound_tasks() if settings.WORKER_NETWORK_SLOTS > 0 else frozenset()
    lane = dict(lease_seconds=settings.JOB_LEASE_SECONDS, poll_interval_seconds=settings.WORKER_POLL_INTERVAL_SECONDS)
    return [
        *(LocalWorker(session_factory, skip=network, **lane) for _ in range(max(general, 1))),
        *(LocalWorker(session_factory, only=network, **lane) for _ in range(max(settings.WORKER_NETWORK_SLOTS, 0))),
    ]
