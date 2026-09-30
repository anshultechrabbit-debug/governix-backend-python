"""In-process token-bucket rate limiting for expensive endpoints.

/ai/ask is the only route that spends money per request, so it gets an explicit
budget. The limiter is deliberately in-process and dependency-free: it needs no
Redis round trip on the hot path and fails open if it is unavailable. Behind
multiple uvicorn workers each worker enforces its own bucket, which bounds the
per-worker rate rather than the global rate -- put a shared limiter at the edge
if a hard global ceiling is required.
"""

import threading
import time
from dataclasses import dataclass

from app.core.exceptions import AppError


class RateLimitExceededError(AppError):
    code = "RATE_LIMITED"
    status_code = 429


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class RateLimiter:
    """Token bucket keyed by an arbitrary string (a principal id, an IP, ...).

    Buckets are evicted once they have been full for longer than one window, so
    the map cannot grow without bound under a stream of unique keys.
    """

    def __init__(self, limit: int, window_seconds: float, *, enabled: bool = True) -> None:
        self.limit = float(limit)
        self.window = float(window_seconds)
        self.enabled = enabled
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def check(self, key: str, *, cost: float = 1.0) -> None:
        """Consume `cost` tokens, raising RateLimitExceededError when empty."""
        if not self.enabled or self.limit <= 0:
            return
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self.limit, updated_at=now)
                self._buckets[key] = bucket
            # Refill for the time elapsed, capped at one full bucket.
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(self.limit, bucket.tokens + elapsed * (self.limit / self.window))
            bucket.updated_at = now
            if bucket.tokens < cost:
                retry_after = max(1, int((cost - bucket.tokens) / (self.limit / self.window)))
                self._evict(now)
                raise RateLimitExceededError(
                    "Too many AI questions. Please wait a moment and try again.",
                    details={"retry_after_seconds": retry_after},
                )
            bucket.tokens -= cost
            self._evict(now)

    def _evict(self, now: float) -> None:
        """Drop buckets that have refilled completely; they carry no state."""
        stale = [key for key, b in self._buckets.items() if now - b.updated_at > self.window]
        for key in stale:
            del self._buckets[key]
