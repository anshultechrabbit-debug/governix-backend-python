import uuid

import pytest

from app.infrastructure.cache import memory as memory_module
from app.infrastructure.cache.base import CacheScope, build_cache_key
from app.infrastructure.cache.memory import MemoryCache

SCOPE = CacheScope(organization_id=uuid.uuid4())


def key(*parts):
    return build_cache_key("test", SCOPE, *parts)


def test_roundtrip_returns_copies():
    cache = MemoryCache(default_ttl_seconds=60, max_entries=10)
    value = {"answer": [1, 2]}
    cache.set(key("q"), value)
    value["answer"].append(3)  # mutating the original must not affect the cache
    assert cache.get(key("q")) == {"answer": [1, 2]}
    cache.delete(key("q"))
    assert cache.get(key("q")) is None


def test_ttl_expiry(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(memory_module.time, "monotonic", lambda: now[0])
    cache = MemoryCache(default_ttl_seconds=60, max_entries=10)
    cache.set(key("a"), 1)
    cache.set(key("b"), 2, ttl_seconds=5)
    now[0] += 6
    assert cache.get(key("b")) is None
    assert cache.get(key("a")) == 1
    now[0] += 60
    assert cache.get(key("a")) is None


def test_lru_eviction():
    cache = MemoryCache(default_ttl_seconds=60, max_entries=2)
    cache.set(key("a"), 1)
    cache.set(key("b"), 2)
    cache.get(key("a"))  # a is now most recently used
    cache.set(key("c"), 3)
    assert cache.get(key("b")) is None
    assert cache.get(key("a")) == 1
    assert cache.get(key("c")) == 3


def test_rejects_non_json_values():
    cache = MemoryCache(default_ttl_seconds=60, max_entries=10)
    with pytest.raises(TypeError):
        cache.set(key("x"), object())
