"""Streaming S3/MinIO storage adapter."""

import hashlib
import io
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from app.core.config import Settings
from app.core.exceptions import NotFoundError
from app.infrastructure.storage.base import Storage, StoredObject, iter_chunks, normalize_key


class S3Storage(Storage):
    """S3-compatible storage; compatible with AWS S3 and MinIO endpoints."""

    def __init__(self, settings: Settings) -> None:
        try:
            import boto3
            from botocore.exceptions import ClientError
        except ImportError as exc:  # pragma: no cover - depends on deployment extras
            raise RuntimeError("STORAGE_BACKEND=s3 requires the production dependencies.") from exc
        self._client_error = ClientError
        self.bucket = settings.S3_BUCKET
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.S3_ENDPOINT,
            region_name=settings.S3_REGION,
            aws_access_key_id=settings.S3_ACCESS_KEY.get_secret_value() if settings.S3_ACCESS_KEY else None,
            aws_secret_access_key=settings.S3_SECRET_KEY.get_secret_value() if settings.S3_SECRET_KEY else None,
        )

    def put(self, key: str, data: BinaryIO | bytes) -> StoredObject:
        key = normalize_key(key)
        digest = hashlib.sha256()
        size = 0
        source: BinaryIO = io.BytesIO(data) if isinstance(data, (bytes, bytearray, memoryview)) else data

        class Stream:
            def read(_, amount: int = -1) -> bytes:
                nonlocal size
                chunk = source.read(amount)
                if chunk:
                    digest.update(chunk)
                    size += len(chunk)
                return chunk

        self.client.upload_fileobj(Stream(), self.bucket, key)
        return StoredObject(key=key, size=size, sha256=digest.hexdigest())

    @contextmanager
    def open(self, key: str) -> Iterator[BinaryIO]:
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=normalize_key(key))["Body"]
        except self._client_error as exc:
            if _missing(exc):
                raise NotFoundError("Object not found.") from None
            raise
        try:
            yield body
        finally:
            body.close()

    @contextmanager
    def local_path(self, key: str) -> Iterator[str]:
        # PDF tooling requires seekable filesystem paths. The temporary file is
        # removed as soon as the caller releases it.
        with tempfile.NamedTemporaryFile(prefix="governix-s3-", suffix=Path(key).suffix, delete=True) as tmp:
            with self.open(key) as source:
                for chunk in iter_chunks(source):
                    tmp.write(chunk)
            tmp.flush()
            yield tmp.name

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=normalize_key(key))
            return True
        except self._client_error as exc:
            if _missing(exc):
                return False
            raise

    def size(self, key: str) -> int:
        try:
            return int(self.client.head_object(Bucket=self.bucket, Key=normalize_key(key))["ContentLength"])
        except self._client_error as exc:
            if _missing(exc):
                raise NotFoundError("Object not found.") from None
            raise

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=normalize_key(key))


def _missing(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    return str(response.get("Error", {}).get("Code", "")) in {"404", "NoSuchKey", "NoSuchObject", "NotFound"}
