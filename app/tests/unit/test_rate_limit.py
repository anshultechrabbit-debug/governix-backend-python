"""The token bucket that protects the paid LLM path."""

import pytest

from app.core.rate_limit import RateLimiter, RateLimitExceededError


def test_requests_within_the_budget_pass():
    limiter = RateLimiter(limit=3, window_seconds=60)
    for _ in range(3):
        limiter.check("user:1")


def test_exceeding_the_budget_raises():
    limiter = RateLimiter(limit=2, window_seconds=60)
    limiter.check("user:1")
    limiter.check("user:1")
    with pytest.raises(RateLimitExceededError) as caught:
        limiter.check("user:1")
    assert caught.value.status_code == 429
    assert caught.value.code == "RATE_LIMITED"
    assert caught.value.details["retry_after_seconds"] >= 1


def test_one_principal_cannot_exhaust_another():
    limiter = RateLimiter(limit=1, window_seconds=60)
    limiter.check("user:1")
    with pytest.raises(RateLimitExceededError):
        limiter.check("user:1")
    limiter.check("user:2")  # a different key has its own budget


def test_the_bucket_refills_over_time():
    limiter = RateLimiter(limit=2, window_seconds=0.05)
    limiter.check("user:1")
    limiter.check("user:1")
    with pytest.raises(RateLimitExceededError):
        limiter.check("user:1")
    import time

    time.sleep(0.06)
    limiter.check("user:1")  # refilled


def test_disabled_limiter_never_blocks():
    limiter = RateLimiter(limit=1, window_seconds=60, enabled=False)
    for _ in range(100):
        limiter.check("user:1")


def test_zero_limit_disables_the_limiter():
    limiter = RateLimiter(limit=0, window_seconds=60)
    for _ in range(10):
        limiter.check("user:1")


def test_idle_buckets_are_evicted():
    limiter = RateLimiter(limit=1, window_seconds=0.02)
    for index in range(50):
        limiter.check(f"user:{index}")
    import time

    time.sleep(0.05)
    limiter.check("user:0")  # triggers eviction of every stale bucket
    assert len(limiter._buckets) <= 1


def test_concurrent_checkers_never_exceed_the_limit():
    import threading

    limiter = RateLimiter(limit=20, window_seconds=60)
    allowed = []
    blocked = []

    def hammer():
        for _ in range(50):
            try:
                limiter.check("user:1")
                allowed.append(1)
            except RateLimitExceededError:
                blocked.append(1)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # The bucket is the authority: never more than the limit is handed out.
    assert len(allowed) <= 20
    assert len(blocked) == 400 - len(allowed)
