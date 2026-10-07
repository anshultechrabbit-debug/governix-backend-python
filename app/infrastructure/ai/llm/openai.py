import json
import logging
import re
import time
from typing import Any

from app.infrastructure.ai import usage as usage_log
from app.infrastructure.ai.credit import CreditPause, transient
from app.infrastructure.ai.llm.base import CALL_ENDS_AT, LLMProvider, LLMResult, LLMUnavailableError

logger = logging.getLogger(__name__)

TRUNCATION_RETRIES = 1  # default when the caller does not pass one
# Only reasoning models accept `reasoning_effort`; sending it to others is a 400.
_REASONING_MODEL = re.compile(r"^(?:gpt-5|o\d|gemini-(?:2\.5|[3-9]))", re.I)  # Gemini 2.5+ think too


# A rate limit or a dropped connection passes within seconds; the client's own retries are
# quick, and under a token-per-minute limit they can all land inside the same minute. These
# waits come on top, before an answer is downgraded to quoting the documents.
TRANSIENT_WAITS = (2.0, 5.0, 10.0)
MAX_WAIT_SECONDS = 15.0
_TRY_AGAIN = re.compile(r"try again in ([\d.]+)\s*(ms|s)\b", re.I)


def _wait_for(exc: Exception, attempt: int) -> float | None:
    """How long to wait before trying again after `exc`, or None when it will not pass."""
    if attempt >= len(TRANSIENT_WAITS) or not transient(exc):
        return None
    wait = TRANSIENT_WAITS[attempt]
    if match := _TRY_AGAIN.search(str(exc)):
        hinted = float(match.group(1)) / (1000 if match.group(2).lower() == "ms" else 1)
        wait = max(wait, hinted + 0.5)
    return min(wait, MAX_WAIT_SECONDS)


def _json_object(raw: str) -> dict[str, Any] | None:
    """The JSON object in a reply, also inside code fences or after a sentence of prose."""
    text = raw.strip()
    for candidate in (text, re.sub(r"^```(?:json)?\s*|\s*```$", "", text), text[text.find("{"):text.rfind("}") + 1]):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


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
        provider: str = "openai",
        schema_in_prompt: bool = False,
    ) -> None:
        if not api_key:
            raise LLMUnavailableError("OPENAI_API_KEY is not configured.")
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=self.MAX_RETRIES)
        self.timeout = timeout
        self.model = model
        self.provider = provider
        self.model_id = f"{provider}:{model}"
        # A server that ignores structured-output schemas: the schema goes into the prompt, the JSON is
        # read out of the reply, and an empty or unreadable reply is asked for again.
        self.schema_in_prompt = schema_in_prompt
        self.max_output_tokens = max_output_tokens
        self.truncation_retries = truncation_retries
        self.reasoning_effort = reasoning_effort
        self._credit = CreditPause("answers and summaries")

    @staticmethod
    def _local(system: str, user: str, schema: dict[str, Any], context) -> LLMResult:
        from app.infrastructure.ai.llm.local import LocalLLM
        return LocalLLM().generate_json(system, user, schema, context=context)

    def _limit(self, budget: int) -> dict[str, Any]:
        """The output cap: OpenAI's own name for it, or the older one other servers implement."""
        return {"max_completion_tokens": budget} if self.provider == "openai" else {"max_tokens": budget}

    def _options(self) -> dict[str, Any]:
        # The model decides, whichever gateway serves it ("gpt-5.4-mini" through OpenCode too).
        if _REASONING_MODEL.match(self.model):
            return {"reasoning_effort": self.reasoning_effort} if self.reasoning_effort else {}
        # Grounded answers want the most likely wording, the same on every run.
        # Reasoning models reject a temperature; others default to 1.0.
        return {"temperature": 0}

    def generate_json(self, system: str, user: str, schema: dict[str, Any], *, context=None) -> LLMResult:
        if self.schema_in_prompt:
            return self._complete_prompted(system, user, schema, context=context)
        return self._complete(system, user, schema, budget=self.max_output_tokens, retries=self.truncation_retries, context=context)

    PROMPTED_ATTEMPTS = 2
    MAX_RETRIES = 3
    # The least time a call is given, even when the request's limit has (nearly) passed.
    MIN_CALL_SECONDS = 2.0

    def _completions(self):
        """The completions API, with a timeout and retries that end by CALL_ENDS_AT when it is set.

        Each try gets the full timeout; with the client's retries, one slow model would otherwise
        keep a reader waiting (1 + retries) times as long as the timeout says."""
        ends_at = CALL_ENDS_AT.get()
        if ends_at is None:
            return self._client.chat.completions
        left = max(ends_at - time.perf_counter(), self.MIN_CALL_SECONDS)
        per_try = min(self.timeout, left)
        retries = max(0, min(self.MAX_RETRIES, int(left // per_try) - 1))
        return self._client.with_options(timeout=per_try, max_retries=retries).chat.completions

    def _complete_prompted(self, system: str, user: str, schema: dict[str, Any], *, context=None) -> LLMResult:
        """Structured output from a model that does not enforce a schema."""
        from openai import OpenAIError

        if self._credit.active:
            return self._local(system, user, schema, context)
        system = (f"{system}\n\nOutput format: reply with ONE JSON object and nothing else (no prose, no code "
                  f"fences), matching this JSON Schema:\n{json.dumps(schema)}")
        started = time.perf_counter()
        raw, response = "", None
        for _attempt in range(self.PROMPTED_ATTEMPTS):
            try:
                response = self._completions().create(
                    model=self.model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    response_format={"type": "json_object"}, **self._limit(self.max_output_tokens), **self._options(),
                )
            except OpenAIError as exc:
                usage_log.record("answers", self.model, error=usage_log.error_code(exc))
                logger.warning("%s LLM request failed (%s: %s). Falling back to LocalLLM.", self.provider, type(exc).__name__, exc)
                self._credit.failed(exc)
                return self._local(system, user, schema, context)
            raw = response.choices[0].message.content or ""
            if (content := _json_object(raw)) is not None:
                usage = response.usage
                usage_log.record("answers", response.model or self.model, input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                                 output_tokens=getattr(usage, "completion_tokens", 0) or 0)
                return LLMResult(
                    content=content, model=response.model or self.model,
                    input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    latency_ms=round((time.perf_counter() - started) * 1000, 1), raw=raw,
                    metadata={"finish_reason": response.choices[0].finish_reason, "schema_in_prompt": True},
                )
            logger.warning("%s returned no JSON object (%d chars); asking again", self.model, len(raw))
        raise LLMUnavailableError(f"{self.model} returned no usable JSON")

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
            waited = 0
            while True:
                try:
                    response = self._completions().create(
                        model=self.model,
                        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                        response_format={
                            "type": "json_schema",
                            "json_schema": {"name": "grounded_answer", "schema": schema, "strict": True},
                        },
                        **self._limit(budget),
                        **options,
                    )
                    break
                except OpenAIError as exc:
                    usage_log.record("answers", self.model, error=usage_log.error_code(exc))
                    if (wait := _wait_for(exc, waited)) is not None:
                        logger.warning("OpenAI LLM request failed (%s); retrying in %.1fs", type(exc).__name__, wait)
                        time.sleep(wait)
                        waited += 1
                        continue
                    logger.warning("%s LLM request failed (%s: %s). Falling back to LocalLLM.", self.provider, type(exc).__name__, exc)
                    self._credit.failed(exc)
                    return self._local(system, user, schema, context)
            choice = response.choices[0]
            spent = response.usage
            usage_log.record("answers", response.model or self.model, input_tokens=getattr(spent, "prompt_tokens", 0),
                         output_tokens=getattr(spent, "completion_tokens", 0))
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

    def _open_stream(self, system: str, user: str, schema: dict[str, Any], options: dict[str, Any]):
        return self._completions().create(
            model=self.model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "grounded_answer", "schema": schema, "strict": True},
            },
            **self._limit(self.max_output_tokens),
            stream=True,
            stream_options={"include_usage": True},
            **options,
        )

    def stream_json(self, system: str, user: str, schema: dict[str, Any], *, context=None):
        """Stream the structured output: text deltas as they arrive, then the LLMResult.

        A model without schema enforcement is asked once, whole: its JSON can only be trusted complete."""
        if self.schema_in_prompt:
            result = self._complete_prompted(system, user, schema, context=context)
            yield result.raw or json.dumps(result.content)
            yield result
            return
        yield from self._stream_json(system, user, schema, context=context)

    def _stream_json(self, system: str, user: str, schema: dict[str, Any], *, context=None):
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
        waited = 0
        while True:
            try:
                stream = self._open_stream(system, user, schema, options)
                break
            except OpenAIError as exc:
                usage_log.record("answers", self.model, error=usage_log.error_code(exc))
                if (wait := _wait_for(exc, waited)) is None:
                    self._credit.failed(exc)
                    logger.warning("OpenAI LLM stream failed (%s: %s). Falling back to LocalLLM.", type(exc).__name__, exc)
                    res = self._local(system, user, schema, context)
                    yield json.dumps(res.content)
                    yield res
                    return
                logger.warning("OpenAI LLM stream failed to start (%s); retrying in %.1fs", type(exc).__name__, wait)
                time.sleep(wait)
                waited += 1
        try:
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
            usage_log.record("answers", self.model, error=usage_log.error_code(exc))
            self._credit.failed(exc)
            if parts:
                # Part of the model's answer was already passed on; appending a
                # different answer to it would splice two answers together.
                logger.warning("OpenAI LLM stream failed midway (%s: %s)", type(exc).__name__, exc)
                raise LLMUnavailableError("The answer stream was interrupted.") from None
            logger.warning("OpenAI LLM stream failed (%s: %s). Falling back to LocalLLM.", type(exc).__name__, exc)
            res = self._local(system, user, schema, context)
            yield json.dumps(res.content)
            yield res
            return
        raw = "".join(parts)
        usage_log.record("answers", model or self.model, input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                         output_tokens=getattr(usage, "completion_tokens", 0) or 0)
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
