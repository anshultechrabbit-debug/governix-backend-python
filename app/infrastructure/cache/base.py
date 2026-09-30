import hashlib
import json
import re
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


@dataclass(frozen=True)
class CacheScope:
    """Who a cached value belongs to. Part of every cache key.

    `permission_scope` is a stable hash of the caller's effective ACL scope and
    `knowledge_version` changes whenever the tenant's indexed content changes, so
    stale or over-privileged entries are never served.
    """

    organization_id: uuid.UUID | None
    branch_id: uuid.UUID | None = None
    department_id: uuid.UUID | None = None
    permission_scope: str = "-"
    knowledge_version: str = "-"

    def __post_init__(self) -> None:
        # Free-form fields must not be able to inject key separators and forge another scope.
        for value in (self.permission_scope, self.knowledge_version):
            if not _SAFE_TOKEN.match(value):
                raise ValueError("Cache scope tokens may only contain [A-Za-z0-9._-].")
        if self.organization_id is None and (
            self.branch_id or self.department_id or self.permission_scope != "-"
        ):
            raise ValueError("System scope cannot carry tenant attributes.")

    @classmethod
    def system(cls) -> "CacheScope":
        """Explicit scope for non-tenant data (e.g. embeddings of identical text)."""
        return cls(organization_id=None)


@dataclass(frozen=True)
class CacheKey:
    value: str


def build_cache_key(namespace: str, scope: CacheScope, *parts: Any) -> CacheKey:
    """Build a tenant-scoped key. Parts are hashed so raw query text never appears in keys."""
    if not namespace or ":" in namespace:
        raise ValueError("Cache namespace must be non-empty and contain no ':'.")
    parts_digest = hashlib.sha256(
        json.dumps(parts, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()
    org = scope.organization_id or "system"
    return CacheKey(
        f"gx:{namespace}:org={org}:br={scope.branch_id or '-'}:dp={scope.department_id or '-'}"
        f":perm={scope.permission_scope}:kv={scope.knowledge_version}:{parts_digest}"
    )


class Cache(ABC):
    """Values must be JSON-serialisable so every backend behaves identically.

    Only `CacheKey` instances are accepted, which forces callers through
    `build_cache_key` and therefore through a tenant scope.
    """

    @abstractmethod
    def get(self, key: CacheKey) -> Any | None: ...

    @abstractmethod
    def set(self, key: CacheKey, value: Any, ttl_seconds: int | None = None) -> None: ...

    @abstractmethod
    def delete(self, key: CacheKey) -> None: ...


def require_key(key: object) -> str:
    if not isinstance(key, CacheKey):
        raise TypeError("Cache keys must be built with build_cache_key().")
    return key.value
