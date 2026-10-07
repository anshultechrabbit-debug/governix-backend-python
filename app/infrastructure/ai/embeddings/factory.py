from app.core.config import Settings
from app.infrastructure.ai.embeddings.base import EmbeddingProvider, EmbeddingUnavailableError


def create_embedder(settings: Settings, cache=None) -> EmbeddingProvider | None:
    """None means embeddings are disabled (keyword + exact search only)."""
    provider = settings.EMBEDDING_PROVIDER.lower()
    if provider == "none":
        return None
    if provider == "local":
        from app.infrastructure.ai.embeddings.local import LocalEmbedding

        return LocalEmbedding(settings.EMBEDDING_DIMENSIONS)
    if provider == "openai":
        from app.infrastructure.ai.embeddings.openai import OpenAIEmbedding

        key = settings.OPENAI_API_KEY.get_secret_value() if settings.OPENAI_API_KEY else None
        return OpenAIEmbedding(key, settings.EMBEDDING_MODEL, settings.EMBEDDING_DIMENSIONS, settings.OPENAI_BASE_URL)
    if provider == "openai_compatible":
        from app.infrastructure.ai.embeddings.openai import OpenAIEmbedding

        key = settings.EMBEDDING_API_KEY or settings.LLM_API_KEY
        base_url = settings.EMBEDDING_BASE_URL or settings.LLM_BASE_URL
        if not base_url:
            raise EmbeddingUnavailableError("EMBEDDING_PROVIDER=openai_compatible needs EMBEDDING_BASE_URL.")
        return OpenAIEmbedding(key.get_secret_value() if key else None, settings.EMBEDDING_MODEL,
                               settings.EMBEDDING_DIMENSIONS, base_url, max_inputs=100,
                               key_setting="EMBEDDING_API_KEY (or LLM_API_KEY)")
    if provider == "ollama":
        from app.infrastructure.ai.embeddings.ollama import OllamaEmbedding

        return OllamaEmbedding(
            settings.EMBEDDING_MODEL, settings.EMBEDDING_DIMENSIONS,
            settings.EMBEDDING_BASE_URL or settings.LLM_BASE_URL or "http://localhost:11434",
            query_prefix=settings.EMBEDDING_QUERY_PREFIX, document_prefix=settings.EMBEDDING_DOCUMENT_PREFIX,
        )
    raise EmbeddingUnavailableError(f"Unknown EMBEDDING_PROVIDER {settings.EMBEDDING_PROVIDER!r}.")
