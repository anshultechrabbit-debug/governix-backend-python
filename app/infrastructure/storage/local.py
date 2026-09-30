import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO

from app.core.exceptions import NotFoundError
from app.infrastructure.storage.base import (
    Storage,
    StoredObject,
    iter_chunks,
    normalize_key,
)

STANDARD_PREFIXES = ("originals", "extracted", "ocr", "pages", "temporary")


class LocalStorage(Storage):
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        for prefix in STANDARD_PREFIXES:
            (self.root / prefix).mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / normalize_key(key)).resolve()
        # Defence in depth against symlinks escaping the storage root.
        if not path.is_relative_to(self.root):
            raise NotFoundError("Object not found.")
        return path

    def put(self, key: str, data: BinaryIO | bytes) -> StoredObject:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        # Write to a temp file in the same directory, then atomically rename,
        # so readers never observe a partially written object.
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as tmp:
                for chunk in iter_chunks(data):
                    digest.update(chunk)
                    size += len(chunk)
                    tmp.write(chunk)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return StoredObject(key=normalize_key(key), size=size, sha256=digest.hexdigest())

    @contextmanager
    def open(self, key: str) -> Iterator[BinaryIO]:
        path = self._path(key)
        try:
            handle = path.open("rb")
        except FileNotFoundError:
            raise NotFoundError("Object not found.") from None
        with handle:
            yield handle

    @contextmanager
    def local_path(self, key: str) -> Iterator[str]:
        path = self._path(key)
        if not path.is_file():
            raise NotFoundError("Object not found.")
        yield str(path)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def size(self, key: str) -> int:
        try:
            return self._path(key).stat().st_size
        except FileNotFoundError:
            raise NotFoundError("Object not found.") from None

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
