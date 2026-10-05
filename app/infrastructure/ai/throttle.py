"""A tokens-per-minute budget shared by every worker thread and process.

An OpenAI account has one tokens-per-minute limit per model. Eight workers that each
send as fast as they can collide on it: most requests come back 429 and wait, and the
account ends up doing about half the work it allows. Each request instead reserves its
tokens here first (a token bucket in one Postgres row) and is told how long to wait for
them, so requests go out at the rate the account allows and are rarely refused.
"""

from datetime import datetime

from sqlalchemy import DateTime, Float, String, text
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from app.core.database import Base

# Stay under the account limit: token counts are estimated before the call, and
# questions asked during a large upload need embeddings too.
USABLE_FRACTION = 0.9
# The most that may go out at once after a quiet spell, as a share of a minute's budget.
BURST_FRACTION = 0.25
CHARS_PER_TOKEN = 4


class RateBucket(Base):
    __tablename__ = "rate_buckets"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    tokens: Mapped[float] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


def estimate_tokens(texts: list[str]) -> int:
    return sum(len(t) for t in texts) // CHARS_PER_TOKEN + len(texts)


# Refill the bucket for the time since its last use, then take the tokens. The balance may go
# negative: the caller waits until it would be back at zero, so callers are served in turn.
_RESERVE = text("""
    INSERT INTO rate_buckets (key, tokens, updated_at) VALUES (:key, :capacity - :amount, clock_timestamp())
    ON CONFLICT (key) DO UPDATE SET
        tokens = least(:capacity, rate_buckets.tokens
                       + :rate * extract(epoch FROM clock_timestamp() - rate_buckets.updated_at)) - :amount,
        updated_at = clock_timestamp()
    RETURNING tokens
""")


def reserve(session_factory: sessionmaker[Session], key: str, amount: int, tokens_per_minute: int) -> float:
    """Take `amount` tokens from the shared budget; returns the seconds to wait before using them."""
    if tokens_per_minute <= 0 or amount <= 0:
        return 0.0
    rate = tokens_per_minute * USABLE_FRACTION / 60
    capacity = tokens_per_minute * USABLE_FRACTION * BURST_FRACTION
    amount = min(amount, capacity)  # a request larger than the burst still goes, after a full wait
    with session_factory() as session, session.begin():
        balance = session.execute(
            _RESERVE, {"key": key, "capacity": capacity, "rate": rate, "amount": amount}
        ).scalar_one()
    return max(-balance, 0.0) / rate
