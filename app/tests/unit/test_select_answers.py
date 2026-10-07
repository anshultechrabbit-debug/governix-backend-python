"""Small-model answering: the model picks sentences, the code quotes them and adds checked arithmetic."""
import uuid
from decimal import Decimal
from types import SimpleNamespace

from app.modules.rag.schema import Claim
from app.modules.rag.select import (
    claims_from_selection, indian_rupees, level_claims, named_picks, numbered_sentences, percent_claims, premise_claim,
    selection_schema, with_other_versions,
)

POLICY = uuid.uuid4()


def item(evidence_id, text, version="1.0", policy=POLICY, name="Car Loan Policy", path="Part 1 > Section 2 Fees"):
    return SimpleNamespace(
        id=evidence_id,
        candidate=SimpleNamespace(text=text, section_path=path),
        source=SimpleNamespace(policy_id=policy, policy_name=name, document_title=name, version_label=version,
                               version_id=f"{policy}-{version}", page_start=4, section_path=path),
    )


def test_sentences_keep_figures_whole_and_leave_out_headings():
    passage = item("E1", "Section 2 Fees - Operating Rule Set 2\n\nCL-2.1 A processing fee of 2% (minimum Rs. 5,000) "
                         "is charged on every car loan above Rs. 3 lakh. CL-2.2 The fee is not refundable.")
    texts = [s.text for s in numbered_sentences([passage])]
    assert texts == ["CL-2.1 A processing fee of 2% (minimum Rs. 5,000) is charged on every car loan above Rs. 3 lakh.",
                     "CL-2.2 The fee is not refundable."]


def test_the_model_picks_ids_before_it_may_declare_nothing_found():
    sentences = numbered_sentences([item("E1", "CL-2.1 A processing fee of 2% is charged on every car loan.")])
    schema = selection_schema(sentences)
    assert list(schema["properties"]) == ["ids", "insufficient_evidence"]
    assert schema["properties"]["ids"]["items"]["enum"] == ["S1"] and schema["properties"]["ids"]["maxItems"] == 4


def test_a_named_clause_is_its_own_answer():
    sentences = numbered_sentences([item("E1", "CL-KEY-01 The limit is 60 days. CL-KEY-011 Another rule applies here.")])
    assert named_picks("What does clause CL-KEY-01 say?", sentences, ["CL-KEY-01"]) == ["S1"]


def test_the_same_rule_is_quoted_from_each_version_and_labelled():
    passages = [item("E1", "CL-KEY-03 The maximum tenure for a car loan is 60 months.", "1.0"),
                item("E2", "CL-KEY-03 The maximum tenure for a car loan is 84 months.", "3.0"),
                item("E3", "CL-9.1 Records shall be kept for 8 years.", "3.0")]
    sentences = numbered_sentences(passages)
    items = {p.id: p for p in passages}
    ids = with_other_versions(["S1"], sentences, items)
    assert ids == ["S1", "S2"]  # the version 3.0 wording of the same rule, not the unrelated rule
    claims = claims_from_selection({"ids": ids}, sentences, items)
    assert [c["text"][:12] for c in claims] == ["Version 1.0:", "Version 3.0:"]


def test_a_mistaken_figure_is_answered_no_and_a_stated_one_yes():
    rule = [Claim(text="The dispute limit is 60 days from the date of return.", citations=[1])]
    assert premise_claim("The dispute limit is 90 days, right?", rule).text == "No: the documents state 60 days, not 90 days."
    assert premise_claim("The dispute limit is 60 days, correct?", rule).text == "Yes: the documents state 60 days."
    assert premise_claim("What is the dispute limit?", rule) is None


def test_a_percentage_is_applied_to_the_readers_amount_but_not_a_yearly_rate():
    fee = [Claim(text="A processing fee of 2% of the loan amount is charged.", citations=[1])]
    assert percent_claims("What fee applies to a Rs. 5 lakh car loan?", fee)[0].text == "2% of Rs. 5 lakh is Rs. 10,000."
    rate = [Claim(text="Penal interest of 2% p.a. is charged on the overdue amount.", citations=[1])]
    assert percent_claims("An EMI of Rs. 40,000 is overdue. What penalty?", rate) == []


def test_the_readers_figure_is_set_against_the_rules_level():
    rule = [Claim(text="Cash deposits of Rs. 10 lakh or more in a month shall be reported.", citations=[3])]
    assert level_claims("A customer deposits Rs. 8 lakh. Report needed?", rule)[0].text == "Rs. 8 lakh is less than Rs. 10 lakh."
    assert level_claims("A customer deposits Rs. 12 lakh. Report needed?", rule)[0].text == "Rs. 12 lakh is more than Rs. 10 lakh."


def test_rupees_are_grouped_as_the_documents_group_them():
    assert [indian_rupees(Decimal(v)) for v in ("5250000", "24000", "800", "100000")] == [
        "Rs. 52,50,000", "Rs. 24,000", "Rs. 800", "Rs. 1,00,000"]


def test_a_passage_header_names_its_own_version_only():
    from app.modules.rag.prompts import evidence_block

    passage = item("E1", "Step 1: Fill in the application form and pay the login fee.", "3", name="Home Loan Guide Version 8")
    passage.source.effective_from = None
    passage.source.page_end = 4
    passage.full_text, passage.amended_by, passage.category_name = passage.candidate.text, [], None
    header = evidence_block(passage).splitlines()[0]
    assert header.startswith("[E1] Home Loan Guide | Version 3 |"), header


def test_of_several_versions_the_newest_that_answers_is_kept():
    from app.modules.rag.service import _newest_claims

    passages = {e.id: e for e in (item("E1", "", "1"), item("E2", "", "5"), item("E3", "", "6"))}
    for p in passages.values():
        p.source.effective_from = None
    claim = lambda text, *ids: SimpleNamespace(text=text, evidence_ids=list(ids))  # noqa: E731
    old = claim("A late payment charge of 2.0% per month is levied on overdue EMIs.", "E1")
    new = claim("Overdue instalments are charged a penalty of 3.0% per month.", "E2")
    unrelated = claim("Floating rate loans are reviewed every quarter.", "E3")
    kept = _newest_claims([old, new, unrelated], passages, ["late", "payment", "penalty", "overdue", "emis"])
    assert kept == [new]  # version 5 restates the rule; version 6 only shares a word with the question
