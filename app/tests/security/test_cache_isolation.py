import uuid

import pytest

from app.infrastructure.cache.base import CacheScope, build_cache_key
from app.infrastructure.cache.memory import MemoryCache

ORG_A, ORG_B = uuid.uuid4(), uuid.uuid4()
BRANCH_A, BRANCH_B = uuid.uuid4(), uuid.uuid4()
DEPT_A, DEPT_B = uuid.uuid4(), uuid.uuid4()
QUERY = "what is the current home loan LTV?"


@pytest.fixture
def cache():
    return MemoryCache(default_ttl_seconds=60, max_entries=100)


@pytest.mark.parametrize("other", [
    CacheScope(ORG_B, BRANCH_A, DEPT_A, "perm1", "kv1"),  # other organisation
    CacheScope(ORG_A, BRANCH_B, DEPT_A, "perm1", "kv1"),  # other branch
    CacheScope(ORG_A, BRANCH_A, DEPT_B, "perm1", "kv1"),  # other department
    CacheScope(ORG_A, BRANCH_A, DEPT_A, "perm2", "kv1"),  # other permission scope
    CacheScope(ORG_A, BRANCH_A, DEPT_A, "perm1", "kv2"),  # knowledge base changed
    CacheScope.system(),
])
def test_same_query_never_crosses_scope(cache, other):
    owner = CacheScope(ORG_A, BRANCH_A, DEPT_A, "perm1", "kv1")
    cache.set(build_cache_key("rag", owner, QUERY), {"answer": "confidential"})
    assert cache.get(build_cache_key("rag", other, QUERY)) is None
    assert cache.get(build_cache_key("rag", owner, QUERY)) == {"answer": "confidential"}


def test_raw_string_keys_are_rejected(cache):
    with pytest.raises(TypeError):
        cache.set("gx:rag:anything", 1)
    with pytest.raises(TypeError):
        cache.get("gx:rag:anything")


@pytest.mark.parametrize("field", ["permission_scope", "knowledge_version"])
def test_scope_tokens_cannot_inject_separators(field):
    with pytest.raises(ValueError):
        CacheScope(ORG_A, **{field: f"x:org={ORG_B}"})


def test_system_scope_cannot_carry_tenant_attributes():
    with pytest.raises(ValueError):
        CacheScope(None, branch_id=BRANCH_A)


def test_query_text_is_not_stored_in_key():
    key = build_cache_key("rag", CacheScope(ORG_A), QUERY)
    assert "LTV" not in key.value and "home" not in key.value
