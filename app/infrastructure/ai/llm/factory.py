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
    raise LLMUnavailableError(f"Unknown LLM_PROVIDER {settings.LLM_PROVIDER!r}.")
