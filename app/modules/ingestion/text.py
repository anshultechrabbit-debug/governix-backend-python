"""Deterministic text normalisation and fingerprints."""

import hashlib
import re
import unicodedata
from collections import deque

_NON_WORD = re.compile(r"[^0-9a-z%.]+")
_WORD = re.compile(r"[0-9a-z%]+(?:\.[0-9]+)?")
SHINGLE_SIZE = 3
SIMHASH_BITS = 64


def normalize_for_hash(text: str) -> str:
    """Case-, whitespace- and punctuation-insensitive form used for content identity."""
    text = unicodedata.normalize("NFKC", text).lower()
    tokens = (token.strip(".") for token in _NON_WORD.sub(" ", text).split())
    return " ".join(token for token in tokens if token)


def words(text: str) -> list[str]:
    return _WORD.findall(unicodedata.normalize("NFKC", text).lower())


class ContentFingerprint:
    """Streams text once to produce both an exact content hash and a SimHash.

    Exact hash: identical text in a different file (re-export, different name).
    SimHash: near-identical text (re-scan, small OCR differences); compared by
    Hamming distance.
    """

    def __init__(self) -> None:
        self._sha = hashlib.sha256()
        self._counts = [0] * SIMHASH_BITS
        self._window: deque[str] = deque(maxlen=SHINGLE_SIZE)
        self._first = True
        self.word_count = 0

    def update(self, text: str) -> None:
        normalized = normalize_for_hash(text)
        if not normalized:
            return
        self._sha.update(((" " if not self._first else "") + normalized).encode())
        self._first = False
        for word in normalized.split():
            self.word_count += 1
            self._window.append(word)
            if len(self._window) == SHINGLE_SIZE:
                self._add_feature(" ".join(self._window))

    def _add_feature(self, feature: str) -> None:
        value = int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=8).digest(), "big")
        for bit in range(SIMHASH_BITS):
            self._counts[bit] += 1 if value >> bit & 1 else -1

    @property
    def content_hash(self) -> str:
        return self._sha.hexdigest()

    @property
    def simhash(self) -> int:
        """Signed 64-bit value so it fits a PostgreSQL BIGINT."""
        value = 0
        for bit, count in enumerate(self._counts):
            if count > 0:
                value |= 1 << bit
        return value - (1 << 64) if value >= 1 << 63 else value


def hamming_distance(a: int, b: int) -> int:
    return ((a ^ b) & ((1 << 64) - 1)).bit_count()


def sha256_text(text: str) -> str:
    return hashlib.sha256(normalize_for_hash(text).encode()).hexdigest()


# Abbreviations common in Indian banking text that end with a period but not a sentence.
_ABBREVIATIONS = ("Rs", "No", "Nos", "Sr", "Dr", "Mr", "Ms", "Mrs", "vs", "Cl", "Sec", "Para", "Ref", "Art", "viz", "etc", "approx", "St", "Ltd", "Co", "Pvt", "p.a", "e.g", "i.e", "w.e.f")
_SENTENCE_BOUNDARY = re.compile(
    r"(?<=[.;!?])"
    + "".join(rf"(?<!\b{re.escape(a)}\.)" for a in _ABBREVIATIONS if "." not in a)
    + r"(?<!\be\.g\.)(?<!\bi\.e\.)(?<!\bp\.a\.)(?<!\b[A-Z]\.)"
    + r"\s+(?=[A-Z0-9(\"'•\-])"
    + r"|\n+"
)


def split_sentences(text: str) -> list[str]:
    """Sentence split that keeps 'Rs. 75 lakh', 'Policy No. 5' and 'e.g. X' intact."""
    return [s.strip() for s in _SENTENCE_BOUNDARY.split(text) if s and s.strip()]


def sentence_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    """Start/end offsets of the sentence containing text[start:end]."""
    left = 0
    for match in _SENTENCE_BOUNDARY.finditer(text, 0, start):
        left = match.end()
    following = _SENTENCE_BOUNDARY.search(text, end)
    right = following.start() if following else len(text)
    return left, right
