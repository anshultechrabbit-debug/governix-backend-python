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
    raise EmbeddingUnavailableError(f"Unknown EMBEDDING_PROVIDER {settings.EMBEDDING_PROVIDER!r}.")
