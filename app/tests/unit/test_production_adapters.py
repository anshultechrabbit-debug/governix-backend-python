import io

import pytest

from app.core.exceptions import NotFoundError
from app.infrastructure.cache.base import CacheScope, build_cache_key
from app.infrastructure.cache.redis import RedisCache
from app.infrastructure.storage.s3 import S3Storage


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}

    def get(self, key): return self.values.get(key)
    def set(self, key, value, ex): self.values[key], self.ttls[key] = value, ex
    def delete(self, key): self.values.pop(key, None)


class ClientError(Exception):
    def __init__(self, code): self.response = {"Error": {"Code": code}}


class FakeS3:
    def __init__(self): self.objects = {}
    def upload_fileobj(self, stream, bucket, key):
        out = bytearray()
        while chunk := stream.read(3): out.extend(chunk)
        self.objects[key] = bytes(out)
    def get_object(self, Bucket, Key):
        if Key not in self.objects: raise ClientError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[Key])}
    def head_object(self, Bucket, Key):
        if Key not in self.objects: raise ClientError("404")
        return {"ContentLength": len(self.objects[Key])}
    def delete_object(self, Bucket, Key): self.objects.pop(Key, None)


def test_redis_adapter_uses_json_and_enforces_cache_keys():
    cache = RedisCache.__new__(RedisCache)
    cache.client, cache.default_ttl_seconds = FakeRedis(), 60
    key = build_cache_key("test", CacheScope.system(), "safe")
    cache.set(key, {"count": 2})
    assert cache.get(key) == {"count": 2}
    assert cache.client.ttls[key.value] == 60
    cache.set(key, {"gone": True}, ttl_seconds=0)
    assert cache.get(key) is None
    with pytest.raises(TypeError): cache.get("unsafe")


def test_s3_adapter_streams_hashes_and_maps_missing_objects():
    storage = S3Storage.__new__(S3Storage)
    storage.client, storage.bucket, storage._client_error = FakeS3(), "bucket", ClientError
    stored = storage.put("originals/org/file.pdf", io.BytesIO(b"abcdefghi"))
    assert stored.size == 9
    assert stored.sha256 == "19cc02f26df43cc571bc9ed7b0c4d29224a3ec229529221725ef76d021c8326f"
    assert storage.size(stored.key) == 9
    with storage.open(stored.key) as body: assert body.read() == b"abcdefghi"
    with pytest.raises(NotFoundError): storage.open("originals/org/missing.pdf").__enter__()
    storage.delete(stored.key)
    assert storage.exists(stored.key) is False
