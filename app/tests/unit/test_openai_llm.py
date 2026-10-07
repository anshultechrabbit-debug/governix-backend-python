import httpx2 as httpx
from openai import RateLimitError

from app.infrastructure.ai.llm import openai as openai_llm
from app.infrastructure.ai.llm.openai import OpenAILLM


class Completions:
    def __init__(self, error):
        self.error, self.calls = error, 0

    def create(self, **_kwargs):
        self.calls += 1
        raise self.error


def llm_failing_with(body: dict) -> tuple[OpenAILLM, Completions]:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    error = RateLimitError("429", response=httpx.Response(429, request=request), body=body)
    llm = OpenAILLM("sk-test", "gpt-5-mini", timeout=1, max_output_tokens=100)
    completions = Completions(error)
    llm._client = type("Client", (), {"chat": type("Chat", (), {"completions": completions})()})()
    return llm, completions


def ask(llm: OpenAILLM):
    return llm.generate_json("s", "u", {}, context={"task": "name", "detected": "Code of Ethics"})


def test_an_account_out_of_credit_is_not_asked_again_for_a_while():
    llm, completions = llm_failing_with({"type": "insufficient_quota", "code": "credit_balance_exhausted"})
    assert ask(llm).content["name"] == "Code of Ethics"  # the local fallback answers
    assert ask(llm).content["name"] == "Code of Ethics"
    assert completions.calls == 1


def test_a_passing_rate_limit_is_retried_on_the_next_call(monkeypatch):
    monkeypatch.setattr(openai_llm.time, "sleep", lambda _seconds: None)
    llm, completions = llm_failing_with({"type": "requests", "code": "rate_limit_exceeded"})
    ask(llm)
    ask(llm)
    # Each call waits and tries again before quoting the documents, and is not paused afterwards.
    assert completions.calls == 2 * (1 + len(openai_llm.TRANSIENT_WAITS))


def test_a_rate_limit_that_passes_still_gets_a_model_answer(monkeypatch):
    waits = []
    monkeypatch.setattr(openai_llm.time, "sleep", waits.append)
    llm, completions = llm_failing_with({"type": "requests", "code": "rate_limit_exceeded"})
    completions.error = RateLimitError(
        "Rate limit reached. Please try again in 2.172s.",
        response=httpx.Response(429, request=httpx.Request("POST", "https://api.openai.com/v1/chat/completions")),
        body={"type": "tokens", "code": "rate_limit_exceeded"},
    )
    answer = type("Response", (), {
        "model": "gpt-5-mini", "usage": None,
        "choices": [type("Choice", (), {"finish_reason": "stop",
                                        "message": type("Message", (), {"content": '{"name": "From the model"}'})()})()],
    })()
    calls = iter([completions.error, answer])

    def create(**_kwargs):
        completions.calls += 1
        outcome = next(calls)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    completions.create = create
    assert ask(llm).content["name"] == "From the model"
    assert waits == [2.672]  # the provider's "try again in 2.172s", plus a margin


def test_opencode_is_an_openai_compatible_provider_with_its_own_key_and_model():
    from app.core.config import Settings
    from app.infrastructure.ai.llm.factory import create_llm

    llm = create_llm(Settings(LLM_PROVIDER="opencode", OPENCODE_API_KEY="oc-test", OPENCODE_MODEL="deepseek-v4.1-flash",
                              OPENAI_API_KEY=None))
    assert llm.model_id == "opencode:deepseek-v4.1-flash"
    assert str(llm._client.base_url).rstrip("/") == "https://opencode.ai/zen/v1"
    assert llm._options() == {"temperature": 0}  # not a reasoning model
    assert llm._limit(100) == {"max_tokens": 100}


def test_payment_required_from_any_provider_pauses_it():
    from types import SimpleNamespace

    from app.infrastructure.ai.credit import out_of_credit

    assert out_of_credit(SimpleNamespace(status_code=402, body={"error": {"message": "Insufficient account funds"}}))
    assert not out_of_credit(SimpleNamespace(status_code=403, body={"type": "error"}))


def test_a_call_and_its_retries_end_by_the_requests_time_limit():
    import time

    from app.infrastructure.ai.llm.base import CALL_ENDS_AT, LLMProvider, LLMResult, TimedLLM

    llm = OpenAILLM("sk-test", "gpt-5-mini", timeout=30, max_output_tokens=100)
    seen = []
    llm._client.with_options = lambda **options: seen.append(options) or llm._client
    token = CALL_ENDS_AT.set(time.perf_counter() + 10)
    try:
        llm._completions()  # 10 s left: one try of at most 10 s, not four of 30 s
    finally:
        CALL_ENDS_AT.reset(token)
    assert seen[0]["max_retries"] == 0 and 9 < seen[0]["timeout"] <= 10
    assert llm._completions() is llm._client.chat.completions  # no limit set: the client's own

    class Probe(LLMProvider):
        def generate_json(self, system, user, schema, *, context=None):
            return LLMResult(content={"limit": CALL_ENDS_AT.get()}, model="probe")

    timed = TimedLLM(Probe(), lambda: 42.0)
    assert timed.generate_json("s", "u", {}).content == {"limit": 42.0}
    assert [p.content for p in timed.stream_json("s", "u", {}) if isinstance(p, LLMResult)] == [{"limit": 42.0}]
    assert CALL_ENDS_AT.get() is None


def test_any_openai_compatible_api_answers_and_embeds_with_its_own_key():
    from app.core.config import Settings
    from app.infrastructure.ai.embeddings.factory import create_embedder
    from app.infrastructure.ai.llm.factory import create_llm

    gemini = "https://generativelanguage.googleapis.com/v1beta/openai/"
    settings = Settings(LLM_PROVIDER="openai_compatible", LLM_BASE_URL=gemini, LLM_API_KEY="g-test",
                        LLM_MODEL="gemini-2.5-flash", LLM_REASONING_EFFORT="low", OPENAI_API_KEY=None,
                        EMBEDDING_PROVIDER="openai_compatible", EMBEDDING_MODEL="gemini-embedding-001")
    llm = create_llm(settings)
    assert llm.model_id == "openai_compatible:gemini-2.5-flash" and not llm.schema_in_prompt
    assert str(llm._client.base_url) == gemini and llm._client.api_key == "g-test"
    assert llm._options() == {"reasoning_effort": "low"}  # a thinking model: no temperature
    embedder = create_embedder(settings)
    assert embedder.model_id == "openai:gemini-embedding-001:1536" and embedder.max_inputs == 100
    assert str(embedder._client.base_url) == gemini and embedder._client.api_key == "g-test"
