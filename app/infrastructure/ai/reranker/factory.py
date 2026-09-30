from app.core.config import Settings
from app.infrastructure.ai.reranker.base import RerankerProvider


def create_reranker(settings: Settings) -> RerankerProvider | None:
    provider = settings.RERANKER_PROVIDER.lower()
    if provider == "none":
        return None
    if provider == "api":
        from app.infrastructure.ai.reranker.api import APIReranker

        if not settings.RERANKER_API_URL:
            raise ValueError("RERANKER_PROVIDER=api requires RERANKER_API_URL.")
        key = settings.RERANKER_API_KEY.get_secret_value() if settings.RERANKER_API_KEY else None
        return APIReranker(settings.RERANKER_API_URL, key, settings.RERANKER_MODEL)
    from app.infrastructure.ai.reranker.local import LocalReranker

    return LocalReranker()
