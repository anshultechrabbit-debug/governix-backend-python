import logging

from app.infrastructure.ai.reranker.base import RerankerProvider

logger = logging.getLogger(__name__)


class APIReranker(RerankerProvider):
    """Cohere/Jina-compatible /rerank endpoint: {model, query, documents} -> results[{index, relevance_score}]."""

    name = "api"

    def __init__(self, url: str, api_key: str | None, model: str | None, timeout: float = 20.0) -> None:
        import httpx2

        self._client = httpx2.Client(timeout=timeout, headers={"Authorization": f"Bearer {api_key}"} if api_key else {})
        self.url = url
        self.model = model
        self.name = f"api:{model or 'default'}"

    def score(self, query: str, passages: list[str]) -> list[float]:
        if not passages:
            return []
        try:
            response = self._client.post(self.url, json={"model": self.model, "query": query, "documents": passages})
            response.raise_for_status()
            scores = [0.0] * len(passages)
            for item in response.json().get("results", []):
                idx = item.get("index")
                if isinstance(idx, int) and 0 <= idx < len(scores):
                    scores[idx] = float(item.get("relevance_score", 0.0))
            return scores
        except Exception as exc:
            # Network errors, timeouts, or non-2xx responses: degrade to zero
            # scores so the reranker is skipped rather than crashing evidence build.
            logger.warning("APIReranker request failed (%s: %s); falling back to zero scores.",
                           type(exc).__name__, exc)
            return [0.0] * len(passages)
