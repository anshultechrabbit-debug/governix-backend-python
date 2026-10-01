"""Record every call to the AI provider, for the usage and credit view.

Providers know nothing about the database; they call `record` after each request
and it writes one row through the configured runtime. Recording never fails the
call it describes.
"""

import logging

logger = logging.getLogger(__name__)


def record(service: str, model: str, *, input_tokens: int = 0, output_tokens: int = 0, error: str | None = None,
           provider: str = "openai") -> None:
    try:
        from app.modules.ai_usage.model import AIUsage
        from app.workers.runtime import get_runtime

        with get_runtime().session_factory() as session:
            session.add(AIUsage(provider=provider, service=service, model=model or "unknown",
                                input_tokens=input_tokens or 0, output_tokens=output_tokens or 0, error=error))
            session.commit()
    except Exception:  # no runtime (scripts, tests without a database) or a write failure
        logger.debug("AI usage not recorded", exc_info=True)


def error_code(exc: Exception) -> str:
    body = exc.body if isinstance(getattr(exc, "body", None), dict) else {}
    return str(getattr(exc, "code", None) or body.get("code") or type(exc).__name__)[:80]
