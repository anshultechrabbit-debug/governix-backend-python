"""Deterministic text normalisation and fingerprints."""

import hashlib
import re
import unicodedata
from collections import Counter

_NON_WORD = re.compile(r"[^0-9a-z%.]+")
_WORD = re.compile(r"[0-9a-z%]+(?:\.[0-9]+)?")
SHINGLE_SIZE = 3
SIMHASH_BITS = 64
# Features hashed before their bits are counted (8 bytes each: bounded memory).
FEATURE_BATCH = 262_144


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
        self._window: list[str] = []
        self._digests = bytearray()  # pending features' 8-byte hashes, counted in batches
        self._first = True
        self.word_count = 0

    def update(self, text: str) -> None:
        normalized = normalize_for_hash(text)
        if not normalized:
            return
        self._sha.update(((" " if not self._first else "") + normalized).encode())
        self._first = False
        tokens = normalized.split()
        self.word_count += len(tokens)
        # Shingles continue across calls: the last words of the previous text start this one's.
        sequence = self._window + tokens
        digest = hashlib.blake2b
        for i in range(len(sequence) - SHINGLE_SIZE + 1):
            feature = " ".join(sequence[i:i + SHINGLE_SIZE]).encode()
            self._digests += digest(feature, digest_size=8).digest()
        self._window = sequence[-(SHINGLE_SIZE - 1):]
        if len(self._digests) >= FEATURE_BATCH * 8:
            self._count_features()

    def _count_features(self) -> None:
        """Add each pending feature's vote (+1 where its bit is set, -1 where not) to every bit.

        Same result as one feature at a time, but counted with C-level loops: a 5,000-page
        document has millions of features, and a Python loop over 64 bits for each one took
        most of the structure stage.
        """
        features = len(self._digests) // 8
        if not features:
            return
        for position in range(8):
            # Byte `position` of a big-endian digest holds bits (7 - position) * 8 .. + 7.
            low_bit = (7 - position) * 8
            for byte_value, count in Counter(self._digests[position::8]).items():
                for k in range(8):
                    if byte_value >> k & 1:
                        self._counts[low_bit + k] += 2 * count
        for bit in range(SIMHASH_BITS):
            self._counts[bit] -= features
        self._digests.clear()

    @property
    def content_hash(self) -> str:
        return self._sha.hexdigest()

    @property
    def simhash(self) -> int:
        """Signed 64-bit value so it fits a PostgreSQL BIGINT."""
        self._count_features()
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
