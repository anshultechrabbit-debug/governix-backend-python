"""Usage and credit: what the AI provider has been used for, what it cost, and what is left.

OpenAI does not let an API key read its credit balance. Spend is therefore either
estimated from the tokens Governix recorded (always available) or, when an
organization Admin key is configured, read from OpenAI's Costs API (exact, and it
includes use of the key outside Governix). "Remaining" is the credit stated in the
settings minus that spend.
"""

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.infrastructure.ai.credit import OUT_OF_CREDIT
from app.modules.ai_usage.model import AIUsage

logger = logging.getLogger(__name__)

# USD per 1M tokens (input, output), OpenAI list prices. Override with OPENAI_PRICES.
PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
    "text-embedding-ada-002": (0.10, 0.0),
}
COSTS_CACHE_SECONDS = 600


class _Row(NamedTuple):
    day: datetime
    service: str
    model: str
    calls: int
    input: int | None
    output: int | None

_costs_cache: dict[str, tuple[float, float]] = {}


def price_of(model: str, overrides: dict[str, list[float]]) -> tuple[float, float] | None:
    """The price of a model, matched by the longest known name it starts with ("gpt-4o-mini-2024-07-18")."""
    table = {**PRICES, **{k: (v[0], v[1] if len(v) > 1 else 0.0) for k, v in overrides.items()}}
    matches = [name for name in table if model == name or model.startswith(f"{name}-")]
    return table[max(matches, key=len)] if matches else None


def _openai_costs(admin_key: str, since: datetime) -> float | None:
    """Exact spend since `since` from OpenAI's Costs API, or None when it cannot be read."""
    cache_key = f"{since.isoformat()}"
    cached = _costs_cache.get(cache_key)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    import httpx

    total, page = 0.0, None
    try:
        for _ in range(20):
            params = {"start_time": int(since.timestamp()), "bucket_width": "1d", "limit": 180}
            if page:
                params["page"] = page
            response = httpx.get("https://api.openai.com/v1/organization/costs", params=params,
                                 headers={"Authorization": f"Bearer {admin_key}"}, timeout=10)
            response.raise_for_status()
            body = response.json()
            total += sum(float(r.get("amount", {}).get("value") or 0)
                         for bucket in body.get("data", []) for r in bucket.get("results", []))
            if not body.get("has_more"):
                break
            page = body.get("next_page")
    except Exception:
        logger.warning("Could not read spend from the OpenAI Costs API; using the estimate", exc_info=True)
        return None
    _costs_cache[cache_key] = (time.monotonic() + COSTS_CACHE_SECONDS, total)
    return total


def usage_overview(session: Session, settings: Settings) -> dict:
    now = datetime.now(UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = today.replace(day=1)
    since = datetime.combine(settings.OPENAI_CREDIT_SINCE, datetime.min.time(), UTC) if settings.OPENAI_CREDIT_SINCE else None
    window = min(filter(None, [since, month, today - timedelta(days=13)]))

    # Days are UTC days, whatever the database session's time zone.
    utc_day = func.date_trunc("day", func.timezone("UTC", AIUsage.created_at))
    rows = session.execute(
        select(
            utc_day.label("day"), AIUsage.service, AIUsage.model,
            func.count().label("calls"),
            func.sum(AIUsage.input_tokens).label("input"), func.sum(AIUsage.output_tokens).label("output"),
        )
        .where(AIUsage.created_at >= window, AIUsage.error.is_(None))
        .group_by(utc_day, AIUsage.service, AIUsage.model)
    ).all()
    rows = [_Row(r.day.replace(tzinfo=UTC), r.service, r.model, r.calls, r.input, r.output) for r in rows]

    unpriced: set[str] = set()

    def cost(model: str, input_tokens: int, output_tokens: int) -> float:
        price = price_of(model, settings.OPENAI_PRICES)
        if price is None:
            unpriced.add(model)
            return 0.0
        return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000

    def totals(start: datetime) -> dict:
        picked = [r for r in rows if r.day >= start]
        return {
            "calls": sum(r.calls for r in picked),
            "input_tokens": sum(r.input or 0 for r in picked),
            "output_tokens": sum(r.output or 0 for r in picked),
            "cost_usd": round(sum(cost(r.model, r.input or 0, r.output or 0) for r in picked), 4),
        }

    by_service: dict[str, dict] = {}
    for r in rows:
        if r.day < month:
            continue
        entry = by_service.setdefault(r.service, {"service": r.service, "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
        entry["calls"] += r.calls
        entry["input_tokens"] += r.input or 0
        entry["output_tokens"] += r.output or 0
        entry["cost_usd"] = round(entry["cost_usd"] + cost(r.model, r.input or 0, r.output or 0), 4)

    days = []
    for offset in range(13, -1, -1):
        day = today - timedelta(days=offset)
        picked = [r for r in rows if r.day.date() == day.date()]
        days.append({
            "date": day.date().isoformat(),
            "tokens": sum((r.input or 0) + (r.output or 0) for r in picked),
            "cost_usd": round(sum(cost(r.model, r.input or 0, r.output or 0) for r in picked), 4),
        })

    # Out of credit when the latest refusal for lack of credit is newer than the latest success.
    last = session.execute(select(
        func.max(case((AIUsage.error.in_(OUT_OF_CREDIT), AIUsage.created_at))),
        func.max(case((AIUsage.error.is_(None), AIUsage.created_at))),
    )).one()
    out_of_credit_at = last[0] if last[0] and (last[1] is None or last[0] > last[1]) else None

    spent, source = None, None
    if since:
        exact = _openai_costs(settings.OPENAI_ADMIN_KEY.get_secret_value(), since) if settings.OPENAI_ADMIN_KEY else None
        spent, source = (exact, "openai") if exact is not None else (totals(since)["cost_usd"], "estimate")
    credit = settings.OPENAI_CREDIT_USD
    remaining = round(max(credit - spent, 0.0), 4) if credit is not None and spent is not None else None

    configured = bool(settings.OPENAI_API_KEY) and "openai" in (settings.LLM_PROVIDER, settings.EMBEDDING_PROVIDER)
    return {
        "provider": "openai",
        "configured": configured,
        "status": "not_configured" if not configured else "out_of_credit" if out_of_credit_at else "active",
        "out_of_credit_at": out_of_credit_at,
        "models": {"answers": settings.LLM_MODEL, "embeddings": settings.EMBEDDING_MODEL},
        "credit_usd": credit,
        "credit_since": settings.OPENAI_CREDIT_SINCE,
        "spent_usd": round(spent, 4) if spent is not None else None,
        "spent_source": source,
        "remaining_usd": remaining,
        "today": totals(today),
        "this_month": totals(month),
        "since_credit": totals(since) if since else None,
        "by_service": sorted(by_service.values(), key=lambda e: e["cost_usd"], reverse=True),
        "daily": days,
        "unpriced_models": sorted(unpriced),
    }
