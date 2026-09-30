import math
import re

from app.infrastructure.ai.reranker.base import RerankerProvider

_WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_STOP = frozenset(
    "a an and are as at be by can do does for from has have how i in is it its of on or shall should "
    "that the their there this to was what when where which who will with current currently".split()
)


def _stem(word: str) -> str:
    return word[:6] if len(word) > 6 else word.rstrip("s")


class LocalReranker(RerankerProvider):
    """Deterministic lexical cross-scorer: query-term coverage (IDF-weighted within
    the candidate set), term proximity and exact-phrase bonus."""

    name = "local-lexical-v1"

    def score(self, query: str, passages: list[str]) -> list[float]:
        terms = [_stem(w) for w in _WORD.findall(query.lower()) if w not in _STOP]
        if not terms or not passages:
            return [0.0] * len(passages)
        tokenized = [[_stem(w) for w in _WORD.findall(p.lower())] for p in passages]
        doc_freq = {t: sum(1 for toks in tokenized if t in toks) for t in set(terms)}
        n = len(passages)
        idf = {t: math.log(1 + (n - df + 0.5) / (df + 0.5)) for t, df in doc_freq.items()}
        total_idf = sum(idf[t] for t in set(terms)) or 1.0
        query_lower = " ".join(_WORD.findall(query.lower()))
        scores = []
        for passage, tokens in zip(passages, tokenized):
            present = [t for t in set(terms) if t in tokens]
            coverage = sum(idf[t] for t in present) / total_idf
            positions = [i for i, tok in enumerate(tokens) if tok in set(terms)]
            proximity = 0.0
            if len(present) > 1 and positions:
                span = max(positions) - min(positions) + 1
                proximity = min(1.0, len(present) * 3 / span)
            phrase = 0.15 if len(query_lower) > 8 and query_lower in " ".join(_WORD.findall(passage.lower())) else 0.0
            scores.append(round(min(1.0, 0.75 * coverage + 0.15 * proximity + phrase), 4))
        return scores
