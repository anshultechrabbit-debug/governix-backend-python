import json
import logging
import re
import time
from typing import Any

from app.infrastructure.ai.credit import CreditPause
from app.infrastructure.ai.llm.base import LLMProvider, LLMResult, LLMUnavailableError

logger = logging.getLogger(__name__)

TRUNCATION_RETRIES = 1  # default when the caller does not pass one
# Only reasoning models accept `reasoning_effort`; sending it to others is a 400.
_REASONING_MODEL = re.compile(r"^(?:gpt-5|o\d)", re.I)


class OpenAILLM(LLMProvider):
    """Structured-JSON generation against the OpenAI chat completions API.

    The truncation retry is a second full call, so the worst-case latency for one
    answer is `timeout * (1 + truncation_retries)`. That product is the number
    that matters for a user waiting, and it is why the retry count is
    configurable rather than hard-coded.
    """

    def __init__(
        self,
        api_key: str | None,
        model: str,
        *,
        timeout: float,
        max_output_tokens: int,
        reasoning_effort: str | None = None,
        base_url: str | None = None,
        truncation_retries: int = 1,
    ) -> None:
        if not api_key:
            raise LLMUnavailableError("OPENAI_API_KEY is not configured.")
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=3)
        self.model = model
        self.model_id = f"openai:{model}"
        self.max_output_tokens = max_output_tokens
        self.truncation_retries = truncation_retries
        self.reasoning_effort = reasoning_effort
        self._credit = CreditPause("answers and summaries")

    @staticmethod
    def _local(system: str, user: str, schema: dict[str, Any], context) -> LLMResult:
        from app.infrastructure.ai.llm.local import LocalLLM
        return LocalLLM().generate_json(system, user, schema, context=context)

    def _options(self) -> dict[str, Any]:
        if self.reasoning_effort and _REASONING_MODEL.match(self.model):
            return {"reasoning_effort": self.reasoning_effort}
        return {}

    def generate_json(self, system: str, user: str, schema: dict[str, Any], *, context=None) -> LLMResult:
        return self._complete(system, user, schema, budget=self.max_output_tokens, retries=self.truncation_retries, context=context)

    def _complete(self, system: str, user: str, schema: dict[str, Any], *, budget: int, retries: int, context=None) -> LLMResult:
        from openai import OpenAIError

        if self._credit.active:
            return self._local(system, user, schema, context)
        options = self._options()
        started = time.perf_counter()
        # Reasoning models spend part of max_completion_tokens thinking. When
        # that exhausts the budget the reply is empty (finish_reason="length"),
        # which must not be mistaken for "the evidence is insufficient".
        choice = None
        response = None
        for attempt in range(retries + 1):
            try:
                response = self._client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": "grounded_answer", "schema": schema, "strict": True},
                    },
                    max_completion_tokens=budget,
                    **options,
                )
            except OpenAIError as exc:
                logger.warning("OpenAI LLM request failed (%s: %s). Falling back to LocalLLM.", type(exc).__name__, exc)
                self._credit.failed(exc)
                return self._local(system, user, schema, context)
            choice = response.choices[0]
            if choice.finish_reason != "length" or attempt == retries:
                break
            logger.warning("LLM output truncated at %s tokens; retrying with a larger budget", budget)
            budget *= 2
        if choice is None or response is None:
            from app.infrastructure.ai.llm.local import LocalLLM
            return LocalLLM().generate_json(system, user, schema, context=context)
        latency = (time.perf_counter() - started) * 1000
        raw = choice.message.content or ""
        try:
            content = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("LLM returned invalid JSON (finish_reason=%s)", choice.finish_reason)
            raise LLMUnavailableError(f"LLM returned no usable output (finish_reason={choice.finish_reason})") from None
        usage = response.usage
        return LLMResult(
            content=content,
            model=response.model or self.model,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=round(latency, 1),
            raw=raw,
            metadata={"finish_reason": choice.finish_reason},
        )

    def stream_json(self, system: str, user: str, schema: dict[str, Any], *, context=None):
        """Stream the structured output: text deltas as they arrive, then the LLMResult.

        A long answer (a list of twenty clauses) takes as long to generate either
        way; streaming lets the caller validate and show each finished claim
        instead of waiting for the last one. A truncated stream falls back to the
        non-streaming call with its larger retry budget.
        """
        from openai import OpenAIError

        if self._credit.active:
            res = self._local(system, user, schema, context)
            yield json.dumps(res.content)
            yield res
            return
        options = self._options()
        started = time.perf_counter()
        parts: list[str] = []
        finish_reason, model, usage = None, None, None
        try:
            stream = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "grounded_answer", "schema": schema, "strict": True},
                },
                max_completion_tokens=self.max_output_tokens,
                stream=True,
                stream_options={"include_usage": True},
                **options,
            )
            for chunk in stream:
                model = chunk.model or model
                if chunk.usage is not None:
                    usage = chunk.usage
                for choice in chunk.choices:
                    if choice.delta and choice.delta.content:
                        parts.append(choice.delta.content)
                        yield choice.delta.content
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
        except OpenAIError as exc:
            logger.warning("OpenAI LLM stream failed (%s: %s). Falling back to LocalLLM.", type(exc).__name__, exc)
            self._credit.failed(exc)
            res = self._local(system, user, schema, context)
            yield json.dumps(res.content)
            yield res
            return
        raw = "".join(parts)
        if finish_reason == "length":
            logger.warning("Streamed LLM output truncated at %s tokens; retrying without streaming", self.max_output_tokens)
            yield self._complete(system, user, schema, budget=self.max_output_tokens * 2,
                                 retries=max(self.truncation_retries - 1, 0))
            return
        try:
            content = json.loads(raw)
        except json.JSONDecodeError:
            raise LLMUnavailableError(f"LLM returned no usable output (finish_reason={finish_reason})") from None
        yield LLMResult(
            content=content,
            model=model or self.model,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            raw=raw,
            metadata={"finish_reason": finish_reason, "streamed": True},
        )
