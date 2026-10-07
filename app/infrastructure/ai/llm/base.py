import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

# When the model call in progress must have ended, as a time.perf_counter() value: a request's time
# limit, which the provider's own timeout and retries must not outlast. None: no such limit.
CALL_ENDS_AT: ContextVar[float | None] = ContextVar("llm_call_ends_at", default=None)


@dataclass
class LLMResult:
    content: dict[str, Any]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: float = 0.0
    raw: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class LLMUnavailableError(Exception):
    """Provider not configured or unreachable after retries."""


class LLMProvider(ABC):
    model_id: str

    @abstractmethod
    def generate_json(self, system: str, user: str, schema: dict[str, Any], *, context: dict[str, Any] | None = None) -> LLMResult:
        """Return a JSON object conforming to `schema`.

        `context` carries structured inputs (question, evidence) that non-LLM
        providers can use directly; API providers only see the prompts.
        """

    def stream_json(
        self, system: str, user: str, schema: dict[str, Any], *, context: dict[str, Any] | None = None
    ) -> Iterator["str | LLMResult"]:
        """Yield the JSON text as it is generated, then the final LLMResult.

        Providers without streaming return the whole text at once; callers treat
        both the same way.
        """
        result = self.generate_json(system, user, schema, context=context)
        yield result.raw or json.dumps(result.content)
        yield result


class TimedLLM(LLMProvider):
    """A provider whose every call ends by a time read as the call starts (see CALL_ENDS_AT).

    The provider is shared by all requests, so the limit cannot be stored on it; it is set around
    each call instead, and only for that call."""

    def __init__(self, llm: LLMProvider, ends_at: Callable[[], float | None]) -> None:
        self._llm = llm
        self._ends_at = ends_at

    def __getattr__(self, name: str) -> Any:
        return getattr(self._llm, name)

    def generate_json(self, system: str, user: str, schema: dict[str, Any], *, context: dict[str, Any] | None = None) -> LLMResult:
        token = CALL_ENDS_AT.set(self._ends_at())
        try:
            return self._llm.generate_json(system, user, schema, context=context)
        finally:
            CALL_ENDS_AT.reset(token)

    def stream_json(
        self, system: str, user: str, schema: dict[str, Any], *, context: dict[str, Any] | None = None
    ) -> Iterator["str | LLMResult"]:
        stream = self._llm.stream_json(system, user, schema, context=context)
        ends_at = self._ends_at()
        while True:
            # A generator's steps may each run in another context (a server's thread pool): set the
            # limit around every step, not once.
            token = CALL_ENDS_AT.set(ends_at)
            try:
                part = next(stream)
            except StopIteration:
                return
            finally:
                CALL_ENDS_AT.reset(token)
            yield part
