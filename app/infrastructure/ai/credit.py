"""An OpenAI account out of credit stays that way until someone pays.

Every call fails, but only after the client's retries, and a 10,000-page upload
makes hundreds of them. Providers stop asking for a while instead and use their
local fallback at once.
"""

import logging
import time

logger = logging.getLogger(__name__)

OUT_OF_CREDIT = frozenset({"insufficient_quota", "credit_balance_exhausted"})
PAUSE_SECONDS = 300.0


def transient(exc: Exception) -> bool:
    """A failure that passes: a rate limit (not an empty account), a timeout, a dropped connection, a 5xx."""
    from openai import APIConnectionError, InternalServerError, RateLimitError

    return isinstance(exc, APIConnectionError | InternalServerError) or (
        isinstance(exc, RateLimitError) and not out_of_credit(exc)
    )


def out_of_credit(exc: Exception) -> bool:
    body = exc.body if isinstance(getattr(exc, "body", None), dict) else {}
    return bool({getattr(exc, "code", None), getattr(exc, "type", None), body.get("code"), body.get("type")} & OUT_OF_CREDIT)


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
            logger.warning("The OpenAI account is out of credit; %s use the local fallback for %d seconds.",
                           self.service, PAUSE_SECONDS)
