"""Redis implementation of the tenant-safe JSON cache contract."""

import json
from typing import Any

from app.core.config import Settings
from app.infrastructure.cache.base import Cache, CacheKey, require_key


class RedisCache(Cache):
    """Redis cache with the exact same serialisation semantics as MemoryCache.

    The import stays local to construction, so a local installation does not
    need the production-only ``redis`` package.
    """

    def __init__(self, settings: Settings) -> None:
        try:
            import redis
        except ImportError as exc:  # pragma: no cover - depends on deployment extras
            raise RuntimeError("CACHE_BACKEND=redis requires the production dependencies.") from exc
        self.default_ttl_seconds = settings.CACHE_DEFAULT_TTL_SECONDS
        self.client = redis.Redis.from_url(settings.REDIS_URL, decode_responses=False)

    def get(self, key: CacheKey) -> Any | None:
        payload = self.client.get(require_key(key))
        if payload is None:
            return None
        return json.loads(payload)

    def set(self, key: CacheKey, value: Any, ttl_seconds: int | None = None) -> None:
        ttl = self.default_ttl_seconds if ttl_seconds is None else ttl_seconds
        raw_key = require_key(key)
        if ttl <= 0:
            self.client.delete(raw_key)
            return
        self.client.set(raw_key, json.dumps(value, separators=(",", ":")), ex=ttl)

    def delete(self, key: CacheKey) -> None:
        self.client.delete(require_key(key))
