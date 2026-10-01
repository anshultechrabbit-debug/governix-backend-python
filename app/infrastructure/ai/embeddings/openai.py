import logging

from app.infrastructure.ai import usage as usage_log
from app.infrastructure.ai.credit import CreditPause, transient
from app.infrastructure.ai.embeddings.base import Embedded, EmbeddingProvider, EmbeddingUnavailableError

logger = logging.getLogger(__name__)

MAX_INPUTS_PER_REQUEST = 256
MAX_CHARS_PER_INPUT = 24_000  # stay well under the model's 8k-token input limit


class OpenAIEmbedding(EmbeddingProvider):
    def __init__(self, api_key: str | None, model: str, dimensions: int, base_url: str | None = None) -> None:
        if not api_key:
            raise EmbeddingUnavailableError("OPENAI_API_KEY is not configured.")
        from openai import OpenAI  # imported lazily: optional at import time

        # Retries absorb short rate limits (the client honours Retry-After); parallel workers hit them.
        self._client = OpenAI(api_key=api_key, base_url=base_url, max_retries=3, timeout=30)
        self.model = model
        self.dimensions = dimensions
        self.model_id = f"openai:{model}:{dimensions}"
        self._credit = CreditPause("embeddings")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.embed(texts).vectors

    def embed(self, texts: list[str]) -> Embedded:
        """OpenAI vectors. A failure that passes (rate limit, timeout) is raised so the job is retried
        and still gets OpenAI vectors; a lasting one (no credit, bad key) falls back to local hashing
        vectors, labelled as such so they are never compared with OpenAI ones."""
        if self._credit.active:
            return self._local(texts)
        vectors: list[list[float]] = []
        try:
            for start in range(0, len(texts), MAX_INPUTS_PER_REQUEST):
                batch = [t[:MAX_CHARS_PER_INPUT] or " " for t in texts[start:start + MAX_INPUTS_PER_REQUEST]]
                response = self._client.embeddings.create(model=self.model, input=batch, dimensions=self.dimensions)
                usage_log.record("embeddings", response.model or self.model,
                                 input_tokens=getattr(response.usage, "prompt_tokens", 0) or 0)
                vectors.extend(item.embedding for item in sorted(response.data, key=lambda d: d.index))
            return Embedded(vectors, self.model_id)
        except Exception as exc:
            usage_log.record("embeddings", self.model, error=usage_log.error_code(exc))
            if transient(exc):
                raise
            logger.warning(
                "OpenAI embeddings call failed (%s: %s). Falling back to local hashing embeddings so ingestion continues.",
                type(exc).__name__, exc,
            )
            self._credit.failed(exc)
            return self._local(texts)

    def _local(self, texts: list[str]) -> Embedded:
        from app.infrastructure.ai.embeddings.local import LocalEmbedding
        fallback = LocalEmbedding(self.dimensions)
        return Embedded(fallback.embed_documents(texts), fallback.model_id)
