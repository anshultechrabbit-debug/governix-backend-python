"""A pool of worker processes for extracting large documents on every CPU core.

Page extraction is CPU-bound Python and PyMuPDF work that holds the interpreter lock,
so worker threads extract one page at a time between them. Processes do not share
that lock: a 10,000-page PDF is extracted on all cores. Small documents are extracted
in the calling thread instead, as starting the pool is not worth it for them.
"""

import atexit
import logging
import multiprocessing
import os
import threading
from concurrent.futures import ProcessPoolExecutor

from app.core.config import Settings

logger = logging.getLogger(__name__)

_POOL: ProcessPoolExecutor | None = None
_LOCK = threading.Lock()


def pool_size(settings: Settings) -> int:
    if settings.EXTRACTION_PROCESSES is not None:
        return max(settings.EXTRACTION_PROCESSES, 0)
    return max(min((os.cpu_count() or 2) - 1, 8), 0)


def extraction_pool(settings: Settings) -> ProcessPoolExecutor | None:
    """The shared pool, started on first use; None when disabled (EXTRACTION_PROCESSES=0)."""
    global _POOL
    size = pool_size(settings)
    if size < 2:
        return None
    with _LOCK:
        if _POOL is None:
            # "spawn": a fresh interpreter per worker, safe to start from a threaded server.
            _POOL = ProcessPoolExecutor(max_workers=size, mp_context=multiprocessing.get_context("spawn"))
            logger.info("Started %s extraction processes", size)
        return _POOL


def discard_pool() -> None:
    """Drop a pool whose worker died; the next large document starts a new one."""
    global _POOL
    with _LOCK:
        pool, _POOL = _POOL, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


atexit.register(discard_pool)
