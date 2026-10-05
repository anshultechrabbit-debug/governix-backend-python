"""Latest version first; an earlier version answers only when the current one cannot."""

import pytest

from app.tests.factories import login, make_tenant
from app.tests.flows import build, confirm_new_policy, confirm_new_version, home_loan_spec, process
from app.tests.pdfs import Section
from app.tests.pipeline import drain

pytestmark = pytest.mark.integration

PREPAYMENT = Section("8", "Prepayment Charges", ["No prepayment charges apply to floating rate home loans."])


@pytest.fixture
def tenant(db):
    return make_tenant(db)


@pytest.fixture
def admin(client, tenant):
    return login(client, tenant.admin)


@pytest.fixture
def home_loan(client, app, tenant, admin):
    """v1 (2024) covers prepayment charges; v2 (2025, in force) dropped that section."""
    v1_doc, analysis = process(client, app, tenant.admin, build(home_loan_spec("1", "01/01/2024", extra_sections=[PREPAYMENT])),
                               filename="home_loan_2024.pdf")
    policy_id = confirm_new_policy(admin, v1_doc, analysis).json()["data"]["policy_id"]
    v2_doc, analysis = process(client, app, tenant.admin, build(home_loan_spec("2", "01/06/2025", ltv="70%")),
                               filename="home_loan_2025.pdf")
    assert confirm_new_version(admin, v2_doc, analysis, policy_id).status_code == 200
    drain(app)
    return policy_id


def ask(session, question, **body):
    response = session.post("/ai/ask", json={"question": question, **body})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_the_version_in_force_answers_when_it_can(admin, home_loan):
    answer = ask(admin, "What is the LTV for home loans above 75 lakh?")
    assert answer["status"] == "answered", answer
    assert {s["version_label"] for s in answer["sources"]} == {"2"}
    assert not any(s["previous_version"] for s in answer["sources"])
    assert "fallback" not in answer["plan"]


def test_previous_version_answers_what_the_current_one_does_not_cover(admin, home_loan):
    answer = ask(admin, "What prepayment charges apply to floating rate home loans?")
    assert answer["status"] == "answered", answer
    assert {s["version_label"] for s in answer["sources"]} == {"1"}
    assert all(s["previous_version"] for s in answer["sources"])
    assert answer["plan"]["fallback"]["used"] is True and answer["plan"]["fallback"]["depth"] == 1
    assert answer["warnings"][0].startswith("The version in force today does not cover this")
    assert "Home Loan Credit Policy v1" in answer["warnings"][0]


def test_explicitly_asking_for_the_current_version_does_not_fall_back(admin, home_loan):
    answer = ask(admin, "What prepayment charges apply to floating rate home loans?", mode="current")
    assert answer["status"] == "no_answer"
    assert "fallback" not in answer["plan"]


def test_no_version_supports_it_so_there_is_no_answer(admin, home_loan):
    answer = ask(admin, "What is the margin requirement for gold loans against jewellery?")
    assert answer["status"] == "no_answer"
    assert answer["no_answer"]["reason"] in ("KEY_TERMS_NOT_FOUND", "LOW_RELEVANCE", "NO_RELEVANT_DOCUMENTS")


def test_fallback_can_be_switched_off(app, admin, home_loan):
    app.state.settings.RAG_PREVIOUS_VERSION_FALLBACK = False
    try:
        answer = ask(admin, "What prepayment charges apply to floating rate home loans?")
    finally:
        app.state.settings.RAG_PREVIOUS_VERSION_FALLBACK = True
    assert answer["status"] == "no_answer"


def test_a_named_file_is_not_answered_from_another_version(admin, home_loan):
    answer = ask(admin, "What prepayment charges apply to floating rate home loans in home_loan_2025.pdf?")
    assert answer["status"] == "no_answer"
    assert "fallback" not in answer["plan"]
    assert answer["plan"]["version_ids"] and "home_loan_2025.pdf" in answer["plan"]["explanation"]


def test_a_named_superseded_file_answers_from_that_file(admin, home_loan):
    answer = ask(admin, "What prepayment charges apply to floating rate home loans in home_loan_2024.pdf?")
    assert answer["status"] == "answered", answer
    assert {s["version_label"] for s in answer["sources"]} == {"1"}
    assert not any(s["previous_version"] for s in answer["sources"])
    assert "fallback" not in answer["plan"]


OLD_PREPAYMENT = Section("8", "Prepayment Charges", ["A prepayment charge of 2% applies to floating rate home loans."])


@pytest.fixture
def six_versions(client, app, tenant, admin):
    """v1 and v2 cover prepayment charges (differently); v3 to v6 (in force) do not."""
    specs = [
        home_loan_spec("1", "01/01/2021", ltv="80%", extra_sections=[OLD_PREPAYMENT]),
        home_loan_spec("2", "01/01/2022", ltv="78%", extra_sections=[PREPAYMENT]),
        home_loan_spec("3", "01/01/2023", ltv="76%"),
        home_loan_spec("4", "01/01/2024", ltv="74%"),
        home_loan_spec("5", "01/01/2025", ltv="72%"),
        home_loan_spec("6", "01/06/2026", ltv="70%"),
    ]
    policy_id = None
    for number, spec in enumerate(specs, start=1):
        doc, analysis = process(client, app, tenant.admin, build(spec), filename=f"home_loan_v{number}.pdf")
        if policy_id is None:
            policy_id = confirm_new_policy(admin, doc, analysis).json()["data"]["policy_id"]
        else:
            assert confirm_new_version(admin, doc, analysis, policy_id).status_code == 200
    drain(app)
    return policy_id


def test_every_earlier_version_is_searched_and_the_latest_one_that_answers_is_used(admin, six_versions):
    answer = ask(admin, "What prepayment charges apply to floating rate home loans?")
    assert answer["status"] == "answered", answer
    assert {s["version_label"] for s in answer["sources"]} == {"2"}  # four versions back, not v1
    assert answer["plan"]["fallback"]["depth"] == 4
    assert "2%" not in answer["answer"]
