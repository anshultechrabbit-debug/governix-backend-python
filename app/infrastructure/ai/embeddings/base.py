import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Embedded:
    vectors: list[list[float]]
    # The model that actually produced the vectors: a provider's fallback may differ from `model_id`.
    model_id: str


class EmbeddingProvider(ABC):
    """Text -> unit-length vectors. `model_id` is stored with every vector so
    vectors from different models are never mixed."""

    model_id: str
    dimensions: int

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed(self, texts: list[str]) -> Embedded:
        """Vectors labelled with the model that produced them. Store that label, not `model_id`."""
        return Embedded(self.embed_documents(texts), self.model_id)

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def query_vector(self, text: str) -> list[float] | None:
        """The query's vector from this provider's own model, or None when only a stand-in was available:
        a stand-in vector would be compared with vectors of another model. A search never fails over it:
        without a vector it runs its keyword lanes only."""
        try:
            embedded = self.embed([text])
        except Exception:
            logger.warning("Query embedding failed; searching without the vector lane", exc_info=True)
            return None
        return embedded.vectors[0] if embedded.model_id == self.model_id else None


class EmbeddingUnavailableError(Exception):
    """The provider is not configured (e.g. missing API key). Not retryable."""
