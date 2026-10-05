import httpx2 as httpx
from openai import RateLimitError

from app.infrastructure.ai.embeddings.openai import OpenAIEmbedding


class Embeddings:
    def __init__(self, error):
        self.error, self.calls = error, 0

    def create(self, **_kwargs):
        self.calls += 1
        raise self.error


def embedder_failing_with(body: dict) -> tuple[OpenAIEmbedding, Embeddings]:
    request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    error = RateLimitError("429", response=httpx.Response(429, request=request), body=body)
    embedder = OpenAIEmbedding("sk-test", "text-embedding-3-small", 64)
    embeddings = Embeddings(error)
    embedder._client = type("Client", (), {"embeddings": embeddings})()
    return embedder, embeddings


OUT_OF_CREDIT = {"type": "insufficient_quota", "code": "credit_balance_exhausted"}


def test_fallback_vectors_are_labelled_with_the_model_that_made_them():
    embedder, _ = embedder_failing_with(OUT_OF_CREDIT)
    embedded = embedder.embed(["Loans above Rs. 75 lakh"])
    assert embedded.model_id == "local-hashing-v1-64" != embedder.model_id
    assert len(embedded.vectors[0]) == 64


def test_an_account_out_of_credit_is_not_asked_again_for_a_while():
    embedder, embeddings = embedder_failing_with(OUT_OF_CREDIT)
    for _ in range(5):
        embedder.embed(["text"])
    assert embeddings.calls == 1


def test_a_stand_in_query_vector_is_never_searched_with():
    embedder, _ = embedder_failing_with({"type": "requests", "code": "rate_limit_exceeded"})
    assert embedder.query_vector("What is the LTV?") is None


def test_a_passing_rate_limit_is_raised_so_the_job_retries_with_openai():
    import pytest
    embedder, _ = embedder_failing_with({"type": "requests", "code": "rate_limit_exceeded"})
    with pytest.raises(RateLimitError):
        embedder.embed(["text"])


def rate_limit(message: str, headers: dict | None = None) -> RateLimitError:
    request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    response = httpx.Response(429, request=request, headers=headers or {})
    return RateLimitError(message, response=response, body={"type": "tokens", "code": "rate_limit_exceeded"})


def test_the_wait_a_rate_limit_asks_for_is_read():
    from app.infrastructure.ai.credit import DEFAULT_RATE_LIMIT_WAIT, rate_limit_wait

    assert rate_limit_wait(rate_limit("429", {"retry-after-ms": "1500"})) == 1.5
    assert rate_limit_wait(rate_limit("429", {"retry-after": "7"})) == 7
    assert rate_limit_wait(rate_limit("Limit 1000000 ... Please try again in 4.848s. Visit")) == 4.848
    assert rate_limit_wait(rate_limit("Please try again in 1m2.5s.")) == 62.5
    assert rate_limit_wait(rate_limit("Please try again in 120ms.")) == 0.12
    assert rate_limit_wait(rate_limit("Rate limit reached.")) == DEFAULT_RATE_LIMIT_WAIT


def test_only_a_passing_rate_limit_has_a_wait():
    from app.infrastructure.ai.credit import rate_limit_wait

    request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
    out_of_credit = RateLimitError("429", response=httpx.Response(429, request=request), body=OUT_OF_CREDIT)
    assert rate_limit_wait(out_of_credit) is None
    assert rate_limit_wait(RuntimeError("boom")) is None
