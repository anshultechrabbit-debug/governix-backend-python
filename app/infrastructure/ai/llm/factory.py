from app.core.config import Settings
from app.infrastructure.ai.llm.base import LLMProvider, LLMUnavailableError


def create_llm(settings: Settings) -> LLMProvider:
    provider = settings.LLM_PROVIDER.lower()
    if provider == "local":
        from app.infrastructure.ai.llm.local import LocalLLM

        return LocalLLM()
    if provider == "openai":
        from app.infrastructure.ai.llm.openai import OpenAILLM

        key = settings.OPENAI_API_KEY.get_secret_value() if settings.OPENAI_API_KEY else None
        return OpenAILLM(
            key, settings.LLM_MODEL, timeout=settings.LLM_TIMEOUT_SECONDS,
            max_output_tokens=settings.LLM_MAX_OUTPUT_TOKENS,
            truncation_retries=settings.LLM_TRUNCATION_RETRIES,
            reasoning_effort=settings.LLM_REASONING_EFFORT, base_url=settings.OPENAI_BASE_URL,
        )
    if provider == "opencode":
        from app.infrastructure.ai.llm.openai import OpenAILLM

        if not settings.OPENCODE_API_KEY:
            raise LLMUnavailableError("OPENCODE_API_KEY is not configured.")
        return OpenAILLM(
            settings.OPENCODE_API_KEY.get_secret_value(), settings.OPENCODE_MODEL or settings.LLM_MODEL,
            timeout=settings.LLM_TIMEOUT_SECONDS, max_output_tokens=settings.LLM_MAX_OUTPUT_TOKENS,
            truncation_retries=settings.LLM_TRUNCATION_RETRIES, reasoning_effort=settings.LLM_REASONING_EFFORT,
            base_url=settings.OPENCODE_BASE_URL, provider="opencode", schema_in_prompt=True,
        )
    if provider == "ollama":
        from app.infrastructure.ai.llm.openai import OpenAILLM

        return OpenAILLM(
            "local", settings.LLM_MODEL, timeout=settings.LLM_TIMEOUT_SECONDS,  # no key, but the client insists
            max_output_tokens=settings.LLM_MAX_OUTPUT_TOKENS,
            truncation_retries=settings.LLM_TRUNCATION_RETRIES,
            base_url=settings.LLM_BASE_URL or "http://localhost:11434/v1", provider="ollama",
        )
    if provider == "openai_compatible":
        from app.infrastructure.ai.llm.openai import OpenAILLM

        if not settings.LLM_BASE_URL:
            raise LLMUnavailableError("LLM_PROVIDER=openai_compatible needs LLM_BASE_URL.")
        key = settings.LLM_API_KEY or settings.OPENAI_API_KEY
        return OpenAILLM(
            key.get_secret_value() if key else "none", settings.LLM_MODEL, timeout=settings.LLM_TIMEOUT_SECONDS,
            max_output_tokens=settings.LLM_MAX_OUTPUT_TOKENS, truncation_retries=settings.LLM_TRUNCATION_RETRIES,
            reasoning_effort=settings.LLM_REASONING_EFFORT, base_url=settings.LLM_BASE_URL,
            provider="openai_compatible", schema_in_prompt=settings.LLM_SCHEMA_IN_PROMPT,
        )
    raise LLMUnavailableError(f"Unknown LLM_PROVIDER {settings.LLM_PROVIDER!r}.")
