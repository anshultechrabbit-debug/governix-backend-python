import pytest
from sqlalchemy import select

from app.infrastructure.ai.llm.base import LLMProvider, LLMResult, LLMUnavailableError
from app.modules.audit.model import AuditEvent
from app.tests.factories import login, make_tenant
from app.tests.flows import V3, V4, build, confirm_new_policy, confirm_new_version, process
from app.tests.pdfs import PolicySpec, Section
from app.tests.pipeline import drain
from app.workers.runtime import get_runtime

pytestmark = pytest.mark.integration


class ScriptedLLM(LLMProvider):
    """Returns a fixed (possibly wrong) model output to exercise validation."""

    model_id = "scripted"

    def __init__(self, claims=None, fail=False):
        self.claims = claims or []
        self.fail = fail
        self.calls = 0

    def generate_json(self, system, user, schema, *, context=None):
        self.calls += 1
        if self.fail:
            raise LLMUnavailableError("down")
        return LLMResult(content={"claims": self.claims, "insufficient_evidence": False, "conflicts": []},
                         model=self.model_id, input_tokens=100, output_tokens=20)


@pytest.fixture
def tenant(db):
    return make_tenant(db)


@pytest.fixture
def admin(client, tenant):
    return login(client, tenant.admin)


@pytest.fixture
def home_loan(client, app, tenant, admin):
    v3_doc, analysis = process(client, app, tenant.admin, build(V3))
    policy_id = confirm_new_policy(admin, v3_doc, analysis).json()["data"]["policy_id"]
    v4_doc, analysis = process(client, app, tenant.admin, build(V4))
    confirm_new_version(admin, v4_doc, analysis, policy_id)
    drain(app)
    return policy_id


def ask(session, question, **body):
    response = session.post("/ai/ask", json={"question": question, **body})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_current_question_is_answered_from_the_version_in_force(db, admin, home_loan):
    answer = ask(admin, "What is the current LTV for home loans above 75 lakh?")
    assert answer["status"] == "answered", answer
    assert "70%" in answer["answer"] and "75%" not in answer["answer"].replace("75 lakh", "")
    assert answer["plan"]["query_class"] == "current"
    source = answer["sources"][0]
    assert (source["policy_name"], source["version_label"], source["section_number"], source["page_start"]) == (
        "Home Loan Credit Policy", "4", "5.2", 1)
    assert all(c["citations"] for c in answer["claims"])
    cited = {n for c in answer["claims"] for n in c["citations"]}
    assert cited == {s["number"] for s in answer["sources"]}  # no uncited sources shown

    event = db.scalar(select(AuditEvent).where(AuditEvent.action == "ai.query"))
    assert event.details["status"] == "answered"
    assert event.details["cited_chunk_ids"] == [s["chunk_id"] for s in answer["sources"]]
    assert event.details["model"] == "local-extractive-v1"
    assert answer["query_id"] == str(event.id)


def test_historical_question_uses_the_version_then_in_force(admin, home_loan):
    answer = ask(admin, "What was the LTV for loans above 75 lakh in March 2025?")
    assert answer["plan"]["query_class"] == "historical" and answer["plan"]["as_of"] == "2025-03-31"
    assert "75%" in answer["answer"]
    assert {s["version_label"] for s in answer["sources"]} == {"3"}


def test_ui_selected_historical_date(admin, home_loan):
    answer = ask(admin, "LTV for loans above 75 lakh", mode="historical", as_of="2025-06-01")
    assert {s["version_label"] for s in answer["sources"]} == {"3"}


def test_comparison_uses_the_deterministic_diff(admin, home_loan):
    answer = ask(admin, "What's changed between v3 and v4 of the home loan credit policy?")
    assert answer["plan"]["query_class"] == "comparison"
    assert answer["status"] == "answered", answer
    assert any(s["kind"] == "comparison" for s in answer["sources"])
    assert "75% → 70%" in answer["answer"] or "70%" in answer["answer"]


def test_unanswerable_question_returns_no_answer_without_calling_the_llm(admin, home_loan):
    scripted = ScriptedLLM()
    get_runtime().overrides["llm"] = scripted
    get_runtime().__dict__.pop("llm", None)
    answer = ask(admin, "What is the margin requirement for gold loans against jewellery?")
    assert answer["status"] == "no_answer"
    assert answer["no_answer"]["reason"] in ("KEY_TERMS_NOT_FOUND", "LOW_RELEVANCE", "NO_RELEVANT_DOCUMENTS")
    assert "gold" in answer["no_answer"]["missing_terms"] or answer["no_answer"]["reason"] != "KEY_TERMS_NOT_FOUND"
    assert answer["no_answer"]["message"].startswith("I couldn't find sufficient supporting information")
    assert scripted.calls == 0


def _use(llm):
    get_runtime().overrides["llm"] = llm
    get_runtime().__dict__.pop("llm", None)


def test_hallucinated_number_is_removed(admin, home_loan):
    _use(ScriptedLLM([{"text": "For loans above Rs. 75 lakh the LTV shall not exceed 80%.", "evidence_ids": ["E1"]}]))
    answer = ask(admin, "What is the LTV for loans above 75 lakh?")
    assert answer["status"] == "no_answer"
    assert answer["no_answer"]["reason"] == "ANSWER_FAILED_VALIDATION"


def test_invalid_claims_are_dropped_and_reported(admin, home_loan):
    _use(ScriptedLLM([
        {"text": "For loans above Rs. 75 lakh the LTV shall not exceed 70%.", "evidence_ids": ["E1"]},
        {"text": "The LTV for NRI customers is 90%.", "evidence_ids": ["E1"]},
        {"text": "Processing fees are waived for all loans.", "evidence_ids": ["E42"]},
    ]))
    answer = ask(admin, "What is the LTV for loans above 75 lakh?")
    assert answer["status"] == "answered"
    assert [c["text"] for c in answer["claims"]] == ["For loans above Rs. 75 lakh the LTV shall not exceed 70%."]
    assert "90%" not in answer["answer"] and "waived" not in answer["answer"]
    assert len([w for w in answer["warnings"] if w.startswith("Removed")]) == 2


def test_llm_outage_degrades_to_no_answer(admin, home_loan):
    _use(ScriptedLLM(fail=True))
    answer = ask(admin, "What is the LTV for loans above 75 lakh?")
    assert answer["no_answer"]["reason"] == "LLM_UNAVAILABLE"


def test_amending_circular_is_surfaced_as_a_conflict(client, app, tenant, admin, home_loan):
    circular = PolicySpec(
        title="CIRCULAR: REVISION OF LTV NORMS",
        header_lines=["Circular No: CRD/2026/45", "Effective Date: 01/08/2026"],
        sections=[Section("1", "Revision of LTV", [
            "Clause 5.2 of the Home Loan Credit Policy is hereby amended. For loans above Rs. 75 lakh "
            "the LTV shall not exceed 65%.",
        ])],
    )
    document_id, analysis = process(client, app, tenant.admin, build(circular))
    target = analysis["amendment_targets"][0]
    confirm_new_policy(admin, document_id, analysis, policy={
        "name": "Circular CRD/2026/45", "category_id": analysis["suggested_category_id"],
        "document_number": "CRD/2026/45",
    }, relationships=[{"relation_type": "AMENDS", "target_policy_id": target["policy_id"], "clauses": ["5.2"]}])
    drain(app)

    answer = ask(admin, "What is the LTV for loans above 75 lakh?")
    assert answer["status"] == "answered", answer
    kinds = {c["type"] for c in answer["conflicts"]}
    assert "AMENDED" in kinds
    titles = {s["policy_name"] for s in answer["sources"]}
    assert {"Home Loan Credit Policy", "Circular CRD/2026/45"} <= titles
    assert answer["warnings"][0].startswith("Sources disagree")


def test_answers_are_cached_per_scope(client, tenant, admin, home_loan):
    first = ask(admin, "What is the interest rate spread?")
    assert first["cache_hit"] is False
    assert ask(admin, "what is the  interest rate SPREAD?")["cache_hit"] is True
    assert ask(login(client, tenant.user_a1), "What is the interest rate spread?")["cache_hit"] is False


def test_query_history_is_personal(client, tenant, admin, home_loan):
    ask(admin, "What is the interest rate spread?")
    assert admin.get("/ai/queries").json()["data"]["total"] == 1
    assert login(client, tenant.user_a1).get("/ai/queries").json()["data"]["total"] == 0


def stream(session, question, **body):
    """POST /ai/ask/stream and return the parsed (event, data) list."""
    import json

    response = session.post("/ai/ask/stream", json={"question": question, **body})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = []
    for block in response.text.split("\n\n"):
        lines = [line for line in block.splitlines() if line and not line.startswith(":")]
        if not lines:
            continue
        kind = next(line[7:] for line in lines if line.startswith("event: "))
        data = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
        events.append((kind, data))
    return events


class StreamingScriptedLLM(ScriptedLLM):
    """Streams the scripted JSON a few characters at a time, like the real provider."""

    def stream_json(self, system, user, schema, *, context=None):
        import json

        result = self.generate_json(system, user, schema, context=context)
        text = json.dumps(result.content)
        for start in range(0, len(text), 7):
            yield text[start:start + 7]
        yield result


def test_stream_emits_progress_validated_claims_then_the_same_answer_as_ask(admin, home_loan):
    events = stream(admin, "What is the current LTV for home loans above 75 lakh?")
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "stage" and kinds[-1] == "done" and "error" not in kinds
    done = events[-1][1]
    streamed = [data for kind, data in events if kind == "claim"]
    assert done["status"] == "answered" and streamed
    # What was shown while streaming is exactly what the final answer confirms.
    assert [(c["text"], c["citations"]) for c in streamed] == [(c["text"], c["citations"]) for c in done["claims"]]
    shown_sources = [s for c in streamed for s in c["sources"]]
    assert [(s["number"], s["chunk_id"]) for s in shown_sources] == [(s["number"], s["chunk_id"]) for s in done["sources"]]
    # And it is the same answer the non-streaming endpoint returns (served from the shared cache).
    again = ask(admin, "What is the current LTV for home loans above 75 lakh?")
    assert again["claims"] == done["claims"] and again["cache_hit"] is True


def test_stream_never_shows_a_claim_that_fails_validation(admin, home_loan):
    _use(StreamingScriptedLLM([
        {"text": "For loans above Rs. 75 lakh the LTV shall not exceed 70%.", "evidence_ids": ["E1"]},
        {"text": "The LTV for NRI customers is 90%.", "evidence_ids": ["E1"]},
        {"text": "Processing fees are waived for all loans.", "evidence_ids": ["E42"]},
    ]))
    events = stream(admin, "What is the LTV for loans above 75 lakh?")
    streamed = [data["text"] for kind, data in events if kind == "claim"]
    assert streamed == ["For loans above Rs. 75 lakh the LTV shall not exceed 70%."]
    assert events[-1][0] == "done" and len([w for w in events[-1][1]["warnings"] if w.startswith("Removed")]) == 2


def test_stream_reports_no_answer_through_done(admin, home_loan):
    _use(StreamingScriptedLLM(fail=True))
    events = stream(admin, "What is the LTV for loans above 75 lakh?")
    assert [kind for kind, _ in events if kind == "claim"] == []
    assert events[-1][0] == "done" and events[-1][1]["no_answer"]["reason"] == "LLM_UNAVAILABLE"


def test_stream_requires_authentication(client, home_loan):
    client.headers.pop("Authorization", None)
    response = client.post("/ai/ask/stream", json={"question": "What is the LTV?"})
    assert response.status_code == 401


@pytest.mark.parametrize("question", ["😂😂😂😂😂", "???", "🙏 !!"])
def test_a_question_without_words_is_refused_not_answered(db, admin, home_loan, question):
    result = ask(admin, question)
    assert result["status"] == "no_answer" and result["sources"] == []
    assert result["no_answer"]["reason"] == "NOT_A_QUESTION"
