import json
from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any


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
