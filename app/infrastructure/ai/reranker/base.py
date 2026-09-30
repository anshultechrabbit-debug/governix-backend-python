from abc import ABC, abstractmethod


class RerankerProvider(ABC):
    name: str

    @abstractmethod
    def score(self, query: str, passages: list[str]) -> list[float]:
        """Relevance of each passage to the query, 0..1, same order as input."""
