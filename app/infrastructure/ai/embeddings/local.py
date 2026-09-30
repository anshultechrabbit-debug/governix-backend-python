import hashlib
import math
import re

from app.infrastructure.ai.embeddings.base import EmbeddingProvider

_TOKEN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?%?")
_STOP = frozenset("a an and are as at be by for from has in is it of on or shall the to was were will with".split())


class LocalEmbedding(EmbeddingProvider):
    """Deterministic feature-hashing embedding (unigrams, bigrams, char trigrams).

    Offline and free, but LEXICAL, not semantic: it captures word overlap, not
    meaning. Intended for development and tests; use an API model in production.
    """

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions
        self.model_id = f"local-hashing-v1-{dimensions}"

    def _features(self, text: str):
        tokens = [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]
        for token in tokens:
            yield token, 1.0
            if len(token) > 4:
                for i in range(len(token) - 2):
                    yield f"#{token[i:i + 3]}", 0.3
        for a, b in zip(tokens, tokens[1:]):
            yield f"{a} {b}", 0.7

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for feature, weight in self._features(text):
                digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "big") % self.dimensions
                sign = 1.0 if digest[4] & 1 else -1.0
                vector[index] += sign * weight
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            vectors.append([v / norm for v in vector])
        return vectors
