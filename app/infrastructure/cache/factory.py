from app.core.config import CacheBackend, Settings
from app.infrastructure.cache.base import Cache


def create_cache(settings: Settings) -> Cache:
    if settings.CACHE_BACKEND is CacheBackend.REDIS:
        from app.infrastructure.cache.redis import RedisCache

        return RedisCache(settings)

    from app.infrastructure.cache.memory import MemoryCache

    return MemoryCache(
        default_ttl_seconds=settings.CACHE_DEFAULT_TTL_SECONDS,
        max_entries=settings.CACHE_MAX_ENTRIES,
    )
