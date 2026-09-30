import hashlib
import io

import pytest

from app.core.exceptions import NotFoundError, ValidationError
from app.infrastructure.storage import base as storage_base
from app.infrastructure.storage.local import LocalStorage


@pytest.fixture
def storage(tmp_path):
    return LocalStorage(tmp_path / "storage")


def test_creates_standard_layout(storage):
    for prefix in ("originals", "extracted", "ocr", "pages", "temporary"):
        assert (storage.root / prefix).is_dir()


def test_put_streams_and_hashes(storage, monkeypatch):
    monkeypatch.setattr(storage_base, "CHUNK_SIZE", 7)  # force multi-chunk streaming
    data = b"governix " * 100
    stored = storage.put("originals/org/doc.pdf", io.BytesIO(data))

    assert stored.size == len(data)
    assert stored.sha256 == hashlib.sha256(data).hexdigest()
    assert storage.exists("originals/org/doc.pdf")
    assert storage.size("originals/org/doc.pdf") == len(data)
    with storage.open("originals/org/doc.pdf") as handle:
        assert handle.read() == data
    with storage.local_path("originals/org/doc.pdf") as path:
        assert open(path, "rb").read() == data


def test_put_overwrites_atomically_without_temp_leftovers(storage):
    storage.put("extracted/a.json", b"one")
    storage.put("extracted/a.json", b"two")
    with storage.open("extracted/a.json") as handle:
        assert handle.read() == b"two"
    assert [p.name for p in (storage.root / "extracted").iterdir()] == ["a.json"]


def test_failed_write_leaves_no_partial_object(storage):
    class Exploding(io.RawIOBase):
        calls = 0

        def read(self, size=-1):
            self.calls += 1
            if self.calls > 1:
                raise OSError("disk gone")
            return b"partial"

    with pytest.raises(OSError):
        storage.put("originals/broken.pdf", Exploding())
    assert not storage.exists("originals/broken.pdf")
    assert list((storage.root / "originals").iterdir()) == []


@pytest.mark.parametrize("key", [
    "", "/etc/passwd", "../outside", "originals/../../outside", "originals//x",
    "originals/./x", "originals\\x", "a/b/",
])
def test_rejects_unsafe_keys(storage, key):
    with pytest.raises(ValidationError):
        storage.put(key, b"x")


def test_symlink_escape_is_blocked(storage, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"secret")
    (storage.root / "originals" / "link").symlink_to(outside)
    with pytest.raises(NotFoundError):
        storage.open("originals/link/secret.txt").__enter__()


def test_missing_objects(storage):
    assert not storage.exists("originals/missing.pdf")
    storage.delete("originals/missing.pdf")  # idempotent
    with pytest.raises(NotFoundError):
        storage.size("originals/missing.pdf")
    with pytest.raises(NotFoundError):
        storage.open("originals/missing.pdf").__enter__()
