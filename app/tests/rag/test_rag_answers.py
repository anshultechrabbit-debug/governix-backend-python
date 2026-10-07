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

    def __init__(self, claims=None, fail=False, insufficient=False):
        self.claims = claims or []
        self.fail = fail
        self.insufficient = insufficient
        self.calls = 0
        self.systems: list[str] = []

    def generate_json(self, system, user, schema, *, context=None):
        self.calls += 1
        self.systems.append(system)
        if self.fail:
            raise LLMUnavailableError("down")
        return LLMResult(
            content={
                "claims": self.claims,
                "summary": "",  # v15: _summary() reads this field; empty = no summary produced
                "insufficient_evidence": self.insufficient,
                "conflicts": [],
            },
            model=self.model_id, input_tokens=100, output_tokens=20,
        )


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
    assert answer["no_answer"]["message"].startswith("None of the documents you can access answer this")
    # At most a rewording of the question is asked for; no answer is ever generated.
    from app.modules.rag.prompts import SYSTEM_PROMPT
    assert SYSTEM_PROMPT not in scripted.systems


def _use(llm):
    get_runtime().overrides["llm"] = llm
    get_runtime().__dict__.pop("llm", None)


def test_hallucinated_number_is_removed(admin, home_loan):
    _use(ScriptedLLM([{"text": "For loans above Rs. 75 lakh the LTV shall not exceed 80%.", "evidence_ids": ["E1"]}]))
    answer = ask(admin, "What is the LTV for loans above 75 lakh?")
    assert answer["status"] == "no_answer"
    assert answer["no_answer"]["reason"] == "ANSWER_FAILED_VALIDATION"


def test_the_model_saying_the_evidence_does_not_answer_wins_over_its_claims(admin, home_loan):
    # Q245 of the policy evaluation: asked about gold loans, the model flagged the evidence as
    # insufficient but also stated the (true) home loan LTV. That is not an answer.
    _use(ScriptedLLM([{"text": "For loans above Rs. 75 lakh the LTV shall not exceed 70%.", "evidence_ids": ["E1"]}],
                     insufficient=True))
    answer = ask(admin, "What is the LTV for loans above 75 lakh?")
    assert answer["status"] == "no_answer"
    assert answer["no_answer"]["reason"] == "INSUFFICIENT_EVIDENCE"


def test_a_temporary_failure_is_tried_again_not_served_from_the_cache(admin, home_loan):
    # A long chat (turns 10 and 12): a timed-out question asked again returned the same failure from the cache.
    down = ScriptedLLM(fail=True)
    _use(down)
    first = ask(admin, "What is the LTV for loans above 75 lakh?")
    assert first["status"] == "no_answer" and first["no_answer"]["reason"] == "LLM_UNAVAILABLE"
    _use(ScriptedLLM([{"text": "For loans above Rs. 75 lakh the LTV shall not exceed 70%.", "evidence_ids": ["E1"]}]))
    again = ask(admin, "What is the LTV for loans above 75 lakh?")
    assert not again["cache_hit"] and again["status"] == "answered"


def test_a_short_follow_up_is_cached_per_conversation(admin, home_loan):
    # "What about mortgage?" after one conversation is not the same question after another.
    _use(ScriptedLLM([{"text": "For loans above Rs. 75 lakh the LTV shall not exceed 70%.", "evidence_ids": ["E1"]}]))
    one = [{"question": "What is the LTV for loans above 75 lakh?", "answer": "70%."}]
    other = [{"question": "Who approves home loans?", "answer": "The credit committee."}]
    first = ask(admin, "What is the limit?", history=one)
    assert not ask(admin, "What is the limit?", history=other)["cache_hit"]
    if first["status"] == "answered":
        assert ask(admin, "What is the limit?", history=one)["cache_hit"]


def test_every_version_a_question_asks_about_is_searched_on_its_own(admin, home_loan, monkeypatch):
    from app.modules.rag import service as rag_service

    seen = []
    original = rag_service.build_evidence

    def capture(*args, **kwargs):
        evidence = original(*args, **kwargs)
        seen.append({item.source.version_label for item in evidence.items})
        return evidence

    monkeypatch.setattr(rag_service, "build_evidence", capture)
    _use(ScriptedLLM([{"text": "For loans above Rs. 75 lakh the LTV shall not exceed 70%.", "evidence_ids": ["E1"]}]))
    ask(admin, "What is the LTV for loans above 75 lakh in all versions of the Home Loan Credit Policy?")
    assert seen and {"3", "4"} <= seen[0]


class _Spanish(ScriptedLLM):
    """Answers in English from the evidence; reads Spanish; translates by marking each statement."""

    def generate_json(self, system, user, schema, *, context=None):
        properties = schema.get("properties", {})
        if "standalone_question" in properties:
            return LLMResult(content={"standalone_question": "What is the LTV for loans above 75 lakh?",
                                      "resolvable": True, "language": "Spanish"}, model=self.model_id)
        if "statements" in properties:
            data = __import__("json").loads(user.split("\n\n", 1)[1])
            return LLMResult(content={"statements": [f"ES: {t}" for t in data["statements"]],
                                      "summary": "", "message": ""}, model=self.model_id)
        return super().generate_json(system, user, schema, context=context)


def test_a_question_in_another_language_is_answered_in_it(admin, home_loan):
    _use(_Spanish([{"text": "For loans above Rs. 75 lakh the LTV shall not exceed 70%.", "evidence_ids": ["E1"]}]))
    answer = ask(admin, "¿Cuál es el LTV para préstamos de más de 75 lakh?")
    assert answer["status"] == "answered", answer
    assert answer["claims"][0]["text"].startswith("ES: ") and "70%" in answer["claims"][0]["text"]
    assert answer["plan"]["answer_language"] == "Spanish"
    assert answer["plan"]["original_claims"][0].startswith("For loans above")


class _Picker(ScriptedLLM):
    """A small model in "select" mode: picks the numbered sentence about the LTV above 75 lakh; its
    one-sentence check answers `verdict`."""

    def __init__(self, verdict=True):
        super().__init__()
        self.verdict = verdict

    def generate_json(self, system, user, schema, *, context=None):
        import re

        properties = schema.get("properties", {})
        if "ids" in properties:
            lines = dict(re.findall(r"^\[(S\d+)\] \(.*?\) (.*)$", user, re.M))
            picked = [i for i, text in lines.items() if "75 lakh" in text and "%" in text][:1]
            return LLMResult(content={"ids": picked, "insufficient_evidence": not picked}, model=self.model_id)
        if "answers" in properties:
            return LLMResult(content={"subject_asked": "LTV", "subject_of_sentence": "LTV", "answers": self.verdict},
                             model=self.model_id)
        return super().generate_json(system, user, schema, context=context)


def test_a_small_model_answers_by_picking_sentences_that_are_quoted_as_written(admin, home_loan, settings):
    settings.RAG_ANSWER_MODE = "select"
    _use(_Picker())
    answer = ask(admin, "What is the LTV for loans above 75 lakh?")
    assert answer["status"] == "answered", answer
    quoted = answer["claims"][0]
    assert "70%" in quoted["text"]
    [source] = [s for s in answer["sources"] if s["number"] in quoted["citations"]]
    assert quoted["text"] in " ".join(source["excerpt"].split())  # word for word, from the passage it cites


def test_a_pick_its_own_check_rejects_is_no_answer(admin, home_loan, settings):
    settings.RAG_ANSWER_MODE = "select"
    _use(_Picker(verdict=False))
    answer = ask(admin, "What is the LTV for loans above 75 lakh?")
    assert answer["status"] == "no_answer" and answer["no_answer"]["reason"] == "INSUFFICIENT_EVIDENCE"


def test_invalid_claims_are_dropped_and_reported(admin, home_loan):
    _use(ScriptedLLM([
        {"text": "For loans above Rs. 75 lakh the LTV shall not exceed 70%.", "evidence_ids": ["E1"]},
        {"text": "The LTV for NRI customers is 90%.", "evidence_ids": ["E1"]},
        {"text": "Processing fees are waived for all loans.", "evidence_ids": ["E42"]},
    ]))
    answer = ask(admin, "What is the LTV for loans above 75 lakh?")
    assert answer["status"] == "answered"
    model_claims = [c["text"] for c in answer["claims"] if "said instead" not in c["text"]]
    assert model_claims == ["For loans above Rs. 75 lakh the LTV shall not exceed 70%."]
    assert "90%" not in answer["answer"] and "waived" not in answer["answer"]
    assert len([c for c in answer["plan"]["checks"] if c.startswith("Removed")]) == 2
    assert not any(w.startswith("Removed") for w in answer["warnings"])


def test_a_rule_that_changed_since_the_previous_version_shows_both(admin, home_loan):
    # The version in force caps the LTV at 70%; the one before it said 75%. A question about the
    # current rule is answered from the version in force and also shows, cited, what changed.
    _use(ScriptedLLM([
        {"text": "For loans above Rs. 75 lakh the LTV shall not exceed 70%.", "evidence_ids": ["E1"]},
    ]))
    answer = ask(admin, "What is the LTV for loans above 75 lakh?")
    assert answer["status"] == "answered"
    current, earlier = answer["claims"]
    assert "70%" in current["text"]
    assert "said instead" in earlier["text"] and "75%" in earlier["text"]
    [cited] = [s for s in answer["sources"] if s["number"] in earlier["citations"]]
    assert cited["previous_version"] is True and cited["version_label"] != answer["sources"][0]["version_label"]
    assert any("earlier version" in w for w in answer["warnings"])


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
    assert answer["warnings"][0].startswith("Your sources disagree")


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
    assert events[-1][0] == "done" and len([c for c in events[-1][1]["plan"]["checks"] if c.startswith("Removed")]) == 2


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


def test_stream_never_shows_claims_that_do_not_answer_the_question(admin, home_loan):
    # True to the evidence, but about something else: never shown, then withdrawn.
    _use(StreamingScriptedLLM([
        {"text": "The interest rate is linked to the repo rate.", "evidence_ids": ["E1"]},
    ]))
    events = stream(admin, "What is the LTV for loans above 75 lakh?")
    assert [kind for kind, _ in events if kind == "claim"] == []
    assert events[-1][1]["status"] == "no_answer"


@pytest.mark.parametrize("question, reason", [
    ("hi", "GREETING"),
    ("Hello Governix!", "GREETING"),
    ("thanks", "GREETING"),
    ("Can you please explain it to me in short?", "NO_SUBJECT"),
])
def test_greetings_and_questions_without_a_subject_are_never_answered_from_documents(admin, home_loan, question, reason):
    scripted = ScriptedLLM([{"text": "For loans above Rs. 75 lakh the LTV shall not exceed 70%.", "evidence_ids": ["E1"]}])
    _use(scripted)
    answer = ask(admin, question)
    assert answer["status"] == "no_answer" and answer["no_answer"]["reason"] == reason
    assert answer["sources"] == [] and answer["claims"] == []
    from app.modules.rag.prompts import SYSTEM_PROMPT
    assert SYSTEM_PROMPT not in scripted.systems
    assert answer["no_answer"]["suggestions"] == []


def test_several_questions_in_one_message_are_all_answered(admin, home_loan, monkeypatch):
    from app.modules.rag import service as rag_service

    questions = ["What is the LTV for loans above 75 lakh?", "What is the interest rate spread?",
                 "What is the LTV for loans above 75 lakh in the home loan policy?", "What is the spread on the repo rate?"]
    monkeypatch.setattr(rag_service, "restated_questions", lambda llm, q, history=None: questions)
    answer = ask(admin, "\n".join(questions))
    assert answer["status"] == "answered"
    assert answer["plan"]["parts"] == questions


def test_a_check_on_the_same_question_is_not_a_second_question():
    from app.modules.rag.service import asks_several

    assert not asks_several("What is the audit cycle limit? Is it 15 months?")
    assert asks_several("What is the LTV? Who approves it?")


@pytest.mark.parametrize("question", [
    "Compare the LTV for loans above 75 lakh in v3 and v4",
    "What is the LTV for loans above 75 lakh in version 3 and version 4?",
    "Has the LTV for loans above 75 lakh changed between the versions?",
])
def test_comparing_a_rule_answers_from_each_version(admin, home_loan, question):
    answer = ask(admin, question)
    assert answer["status"] == "answered", answer
    assert answer["plan"]["query_class"] == "comparison"
    assert {s["version_label"] for s in answer["sources"]} == {"3", "4"}
    assert not any(s["kind"] == "comparison" for s in answer["sources"])  # the rule, not the whole diff
    assert "75%" in answer["answer"] and "70%" in answer["answer"]


@pytest.mark.parametrize("question", ["What changed in v4?", "What changed in version 3.0?"])
def test_what_changed_in_one_version_compares_it_with_its_neighbour(admin, home_loan, question):
    answer = ask(admin, question)
    assert answer["status"] == "answered", answer
    assert any(s["kind"] == "comparison" and s["version_label"] == "3 → 4" for s in answer["sources"])


class _TimedOut(ScriptedLLM):
    """The configured model timed out; the local stand-in answered and found nothing to quote."""

    def generate_json(self, system, user, schema, *, context=None):
        from app.infrastructure.ai.llm.local import LocalLLM

        result = super().generate_json(system, user, schema, context=context)
        if "claims" in schema.get("properties", {}):
            result.model = LocalLLM.model_id
            result.content["insufficient_evidence"] = True
        return result


def test_a_timed_out_model_is_reported_as_such_not_as_not_found(admin, home_loan):
    _use(_TimedOut([]))
    answer = ask(admin, "What is the LTV for loans above 75 lakh?")
    assert answer["status"] == "no_answer"
    assert answer["no_answer"]["reason"] == "LLM_UNAVAILABLE"
    assert answer["no_answer"]["suggestions"] == []  # no "name the policy" advice for an outage
