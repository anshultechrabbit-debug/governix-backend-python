import hashlib
import random

import pytest

from app.modules.ingestion import text
from app.modules.ingestion.text import ContentFingerprint, normalize_for_hash


def reference_simhash(parts: list[str]) -> int:
    """The SimHash as first defined: every 3-word shingle votes on each of the 64 bits.
    Stored fingerprints were made this way, so the fast version must give the same value."""
    words = [w for part in parts for w in normalize_for_hash(part).split()]
    counts = [0] * 64
    for i in range(len(words) - 2):
        value = int.from_bytes(hashlib.blake2b(" ".join(words[i:i + 3]).encode(), digest_size=8).digest(), "big")
        for bit in range(64):
            counts[bit] += 1 if value >> bit & 1 else -1
    value = sum(1 << bit for bit, count in enumerate(counts) if count > 0)
    return value - (1 << 64) if value >= 1 << 63 else value


@pytest.mark.parametrize("batch", [1, 7, text.FEATURE_BATCH])
def test_simhash_is_unchanged_however_the_text_arrives(monkeypatch, batch):
    monkeypatch.setattr(text, "FEATURE_BATCH", batch)
    rng = random.Random(batch)
    vocab = ["loan", "LTV", "Rs.", "75", "lakh", "shall", "not", "exceed", "70%", "KYC", "", " ", "Section 5.2"]
    for _ in range(200):
        parts = [" ".join(rng.choice(vocab) for _ in range(rng.randint(0, 9))) for _ in range(rng.randint(0, 12))]
        fingerprint = ContentFingerprint()
        for part in parts:
            fingerprint.update(part)
        assert fingerprint.simhash == reference_simhash(parts), parts
        assert fingerprint.word_count == sum(len(normalize_for_hash(p).split()) for p in parts)
