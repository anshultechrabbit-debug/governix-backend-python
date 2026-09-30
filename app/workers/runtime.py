"""Shared infrastructure for task handlers.

Handlers receive only (payload, JobContext); they reach storage, the database
and AI providers through the runtime configured at process start (API process
for in-process workers, `python -m app.workers` otherwise).
"""

from dataclasses import dataclass, field
from functools import cached_property

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.infrastructure.cache.base import Cache
from app.infrastructure.queue.base import Queue
from app.infrastructure.storage.base import Storage


@dataclass
class Runtime:
    settings: Settings
    session_factory: sessionmaker[Session]
    storage: Storage
    queue: Queue
    cache: Cache
    overrides: dict = field(default_factory=dict)

    @cached_property
    def ocr(self):
        if "ocr" in self.overrides:
            return self.overrides["ocr"]
        from app.infrastructure.ai.ocr.factory import create_ocr

        return create_ocr(self.settings)

    @cached_property
    def embedder(self):
        if "embedder" in self.overrides:
            return self.overrides["embedder"]
        from app.infrastructure.ai.embeddings.factory import create_embedder

        return create_embedder(self.settings, self.cache)

    @cached_property
    def reranker(self):
        if "reranker" in self.overrides:
            return self.overrides["reranker"]
        from app.infrastructure.ai.reranker.factory import create_reranker

        return create_reranker(self.settings)

    @cached_property
    def llm(self):
        if "llm" in self.overrides:
            return self.overrides["llm"]
        from app.infrastructure.ai.llm.factory import create_llm

        return create_llm(self.settings)


_runtime: Runtime | None = None


def configure_runtime(runtime: Runtime) -> Runtime:
    global _runtime
    _runtime = runtime
    return runtime


def get_runtime() -> Runtime:
    if _runtime is None:
        raise RuntimeError("Worker runtime is not configured.")
    return _runtime
