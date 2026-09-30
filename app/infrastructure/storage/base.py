from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import BinaryIO

from app.core.exceptions import ValidationError

CHUNK_SIZE = 1024 * 1024  # 1 MiB streaming buffer; files are never read fully into memory


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    sha256: str


class Storage(ABC):
    """Object storage for original documents and derived artifacts.

    Keys are POSIX-style relative paths such as ``originals/<org_id>/<doc_id>.pdf``.
    PostgreSQL stores only keys and metadata, never file bytes.
    """

    @abstractmethod
    def put(self, key: str, data: BinaryIO | bytes) -> StoredObject:
        """Stream `data` to `key`, computing size and SHA-256 on the way. Overwrites."""

    @abstractmethod
    def open(self, key: str) -> AbstractContextManager[BinaryIO]:
        """Open `key` for streaming binary reads."""

    @abstractmethod
    def local_path(self, key: str) -> AbstractContextManager[str]:
        """Yield a filesystem path for `key`.

        PDF libraries need a real path to open huge files lazily. Remote backends
        download to a temporary file for the duration of the context.
        """

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def size(self, key: str) -> int: ...

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete `key`. Deleting a missing key is not an error."""


def normalize_key(key: str) -> str:
    """Reject absolute paths, traversal and empty segments; return a canonical key."""
    if not key or "\\" in key or "\x00" in key:
        raise ValidationError("Invalid storage key.")
    path = PurePosixPath(key)
    if path.is_absolute() or any(part in ("", ".", "..") for part in key.split("/")):
        raise ValidationError("Invalid storage key.")
    return str(path)


def iter_chunks(data: BinaryIO | bytes) -> Iterator[bytes]:
    if isinstance(data, (bytes, bytearray, memoryview)):
        view = memoryview(data)
        for start in range(0, len(view), CHUNK_SIZE):
            yield bytes(view[start : start + CHUNK_SIZE])
        return
    while chunk := data.read(CHUNK_SIZE):
        yield chunk
