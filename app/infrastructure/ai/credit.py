"""An OpenAI account out of credit stays that way until someone pays.

Every call fails, but only after the client's retries, and a 10,000-page upload
makes hundreds of them. Providers stop asking for a while instead and use their
local fallback at once.
"""

import logging
import re
import time

logger = logging.getLogger(__name__)

OUT_OF_CREDIT = frozenset({"insufficient_quota", "credit_balance_exhausted", "FreeUsageLimitError"})
PAUSE_SECONDS = 300.0


def transient(exc: Exception) -> bool:
    """A failure that passes: a rate limit (not an empty account), a timeout, a dropped connection, a 5xx."""
    from openai import APIConnectionError, InternalServerError, RateLimitError

    return isinstance(exc, APIConnectionError | InternalServerError) or (
        isinstance(exc, RateLimitError) and not out_of_credit(exc)
    )


DEFAULT_RATE_LIMIT_WAIT = 10.0
_TRY_AGAIN = re.compile(r"try again in (?:(\d+)m(?!s))?(?:([\d.]+)(ms|s))?")


def rate_limit_wait(exc: Exception) -> float | None:
    """Seconds OpenAI asks a rate-limited caller to wait, or None if `exc` is not a practical rate limit."""
    from openai import RateLimitError

    if not isinstance(exc, RateLimitError) or out_of_credit(exc):
        return None
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        if "retry-after-ms" in headers:
            return float(headers["retry-after-ms"]) / 1000
        if "retry-after" in headers:
            return float(headers["retry-after"])
    except ValueError:
        pass
    match = _TRY_AGAIN.search(str(exc))
    if match and (match.group(1) or match.group(2)):
        minutes, amount, unit = match.groups()
        seconds = float(amount or 0) / (1000 if unit == "ms" else 1)
        return int(minutes or 0) * 60 + seconds
    return DEFAULT_RATE_LIMIT_WAIT


def out_of_credit(exc: Exception) -> bool:
    """OpenAI's out-of-credit codes, or HTTP 402 Payment Required / FreeUsageLimitError (OpenCode Zen)."""
    if getattr(exc, "status_code", None) == 402:
        return True
    body = exc.body if isinstance(getattr(exc, "body", None), dict) else {}
    error_obj = body.get("error") if isinstance(body.get("error"), dict) else {}
    types = {
        getattr(exc, "code", None),
        getattr(exc, "type", None),
        body.get("code"),
        body.get("type"),
        error_obj.get("type"),
        error_obj.get("code"),
    }
    if "FreeUsageLimitError" in str(exc) or "Insufficient account funds" in str(exc):
        return True
    return bool(types & OUT_OF_CREDIT)


class CreditPause:
    """Remembers an out-of-credit answer so the next calls skip OpenAI for a while."""

    def __init__(self, service: str) -> None:
        self.service = service
        self._until = 0.0

    @property
    def active(self) -> bool:
        return time.monotonic() < self._until

    def failed(self, exc: Exception) -> None:
        if out_of_credit(exc):
            self._until = time.monotonic() + PAUSE_SECONDS
            logger.warning("The AI provider account is out of credit or funds; %s use the local fallback for %d seconds.",
                           self.service, PAUSE_SECONDS)
