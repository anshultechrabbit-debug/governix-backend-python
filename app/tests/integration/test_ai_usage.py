"""The dashboard's AI usage and credit view."""

from datetime import UTC, date, datetime

import pytest

from app.infrastructure.ai import usage
from app.modules.ai_usage.model import AIUsage
from app.modules.ai_usage.service import price_of
from app.tests.factories import login, make_tenant

pytestmark = pytest.mark.integration


@pytest.fixture
def tenant(db):
    return make_tenant(db)


def test_prices_match_dated_model_names_and_can_be_overridden():
    assert price_of("gpt-4o-mini-2024-07-18", {}) == (0.15, 0.60)
    assert price_of("gpt-4o-2024-08-06", {}) == (2.50, 10.00)
    assert price_of("text-embedding-3-small", {}) == (0.02, 0.0)
    assert price_of("my-model", {"my-model": [1.0, 2.0]}) == (1.0, 2.0)
    assert price_of("unknown-model", {}) is None


def test_usage_and_remaining_credit(app, client, db, tenant):
    settings = app.state.settings
    settings.OPENAI_CREDIT_USD, settings.OPENAI_CREDIT_SINCE = 5.0, date(2026, 1, 1)
    try:
        usage.record("answers", "gpt-4o-mini-2024-07-18", input_tokens=1_000_000, output_tokens=1_000_000)
        usage.record("embeddings", "text-embedding-3-small", input_tokens=2_000_000)
        data = login(client, tenant.admin).get("/dashboard/ai-usage").json()["data"]
    finally:
        settings.OPENAI_CREDIT_USD, settings.OPENAI_CREDIT_SINCE = None, None
    assert data["today"]["input_tokens"] == 3_000_000 and data["today"]["output_tokens"] == 1_000_000
    assert data["spent_usd"] == pytest.approx(0.79) and data["spent_source"] == "estimate"
    assert data["remaining_usd"] == pytest.approx(4.21)
    assert {s["service"] for s in data["by_service"]} == {"answers", "embeddings"}


def test_out_of_credit_is_reported_until_a_call_succeeds_again(client, db, tenant):
    admin = login(client, tenant.admin)
    usage.record("answers", "gpt-4o-mini", error="insufficient_quota")
    assert admin.get("/dashboard/ai-usage").json()["data"]["out_of_credit_at"]
    db.add(AIUsage(provider="openai", service="answers", model="gpt-4o-mini", input_tokens=1, output_tokens=1,
                   created_at=datetime.now(UTC).replace(year=2099)))
    db.commit()
    assert admin.get("/dashboard/ai-usage").json()["data"]["out_of_credit_at"] is None


def test_only_administrators_see_it(client, tenant):
    assert login(client, tenant.manager_a).get("/dashboard/ai-usage").status_code == 403
    assert login(client, tenant.user_a1).get("/dashboard/ai-usage").status_code == 403
