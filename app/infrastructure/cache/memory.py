import json
import threading
import time
from collections import OrderedDict
from typing import Any

from app.infrastructure.cache.base import Cache, CacheKey, require_key


class MemoryCache(Cache):
    """Process-local LRU cache with TTL. Values are stored serialised, like Redis."""

    def __init__(self, default_ttl_seconds: int, max_entries: int) -> None:
        self.default_ttl_seconds = default_ttl_seconds
        self.max_entries = max_entries
        self._entries: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: CacheKey) -> Any | None:
        raw_key = require_key(key)
        with self._lock:
            entry = self._entries.get(raw_key)
            if entry is None:
                return None
            expires_at, payload = entry
            if expires_at <= time.monotonic():
                del self._entries[raw_key]
                return None
            self._entries.move_to_end(raw_key)
        return json.loads(payload)

    def set(self, key: CacheKey, value: Any, ttl_seconds: int | None = None) -> None:
        raw_key = require_key(key)
        payload = json.dumps(value)
        ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        with self._lock:
            self._entries[raw_key] = (time.monotonic() + ttl, payload)
            self._entries.move_to_end(raw_key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def delete(self, key: CacheKey) -> None:
        raw_key = require_key(key)
        with self._lock:
            self._entries.pop(raw_key, None)
