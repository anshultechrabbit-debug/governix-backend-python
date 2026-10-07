import json
import logging
import math
import urllib.error
import urllib.request

from app.infrastructure.ai.embeddings.base import Embedded, EmbeddingProvider, EmbeddingUnavailableError

logger = logging.getLogger(__name__)

MAX_INPUTS_PER_REQUEST = 64


class OllamaEmbedding(EmbeddingProvider):
    """A local embedding model served by Ollama (e.g. nomic-embed-text), for semantic search without
    an API account.

    The model's vectors (768 numbers for nomic-embed-text) are padded with zeros to the width of the
    vector column: zero padding changes neither the length of a vector nor the cosine similarity of
    two of them, so they are stored and searched like any other, with no migration. They are labelled
    with this model, so they are never compared with another model's vectors.

    Retrieval models are often trained with task prefixes ("search_query: " / "search_document: ");
    those are added here, to the query and to the documents respectively.
    """

    def __init__(self, model: str, dimensions: int, base_url: str, *, query_prefix: str = "",
                 document_prefix: str = "", timeout: float = 120.0) -> None:
        self.model = model
        self.dimensions = dimensions
        self.base_url = base_url.rstrip("/").removesuffix("/v1")
        self.query_prefix = query_prefix
        self.document_prefix = document_prefix
        self.timeout = timeout
        self.model_id = f"ollama:{model}:{dimensions}"

    def _request(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), MAX_INPUTS_PER_REQUEST):
            body = json.dumps({"model": self.model, "input": texts[start:start + MAX_INPUTS_PER_REQUEST],
                               "truncate": True}).encode()
            request = urllib.request.Request(f"{self.base_url}/api/embed", body, {"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    vectors += json.loads(response.read())["embeddings"]
            except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
                raise EmbeddingUnavailableError(f"Ollama embeddings failed: {exc}") from exc
        return [self._fit(v) for v in vectors]

    def _fit(self, vector: list[float]) -> list[float]:
        if len(vector) > self.dimensions:
            raise EmbeddingUnavailableError(
                f"{self.model} returns {len(vector)} dimensions; the vector column holds {self.dimensions}.")
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector] + [0.0] * (self.dimensions - len(vector))

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._request([f"{self.document_prefix}{t}" for t in texts])

    def embed_query(self, text: str) -> list[float]:
        return self._request([f"{self.query_prefix}{text}"])[0]

    def query_vector(self, text: str) -> list[float] | None:
        try:
            return self.embed_query(text)
        except EmbeddingUnavailableError:
            logger.warning("Query embedding failed; searching without the vector lane", exc_info=True)
            return None

    def embed(self, texts: list[str]) -> Embedded:
        return Embedded(self.embed_documents(texts), self.model_id)
