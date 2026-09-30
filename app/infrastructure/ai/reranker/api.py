from app.infrastructure.ai.reranker.base import RerankerProvider


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
        response = self._client.post(self.url, json={"model": self.model, "query": query, "documents": passages})
        response.raise_for_status()
        scores = [0.0] * len(passages)
        for item in response.json().get("results", []):
            scores[item["index"]] = float(item.get("relevance_score", 0.0))
        return scores
