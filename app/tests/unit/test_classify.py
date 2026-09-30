import uuid

from app.modules.categories.defaults import DEFAULT_CATEGORIES
from app.modules.ingestion.analysis.classify import CategoryProfile, classify

CATEGORIES = [CategoryProfile(uuid.uuid4(), name, tuple(kw)) for name, _slug, _type, _rank, kw in DEFAULT_CATEGORIES]


def test_policy_document_is_classified_as_policy():
    result = classify(
        CATEGORIES,
        "Home Loan Credit Policy",
        "HOME LOAN CREDIT POLICY\nPolicy No: HL-2025-01\nApproved by the Board",
        "This policy sets out credit standards. Scope of the policy covers all home loans.",
    )
    assert result.name == "Policies"
    assert result.confidence >= 0.8


def test_circular_is_classified_as_circular():
    result = classify(
        CATEGORIES,
        "Circular on Revision of LTV Norms",
        "CIRCULAR\nCircular No: CRD/2026/45\nTo: All Branches",
        "All branches are hereby informed that clause 5.2 of the home loan policy is hereby amended "
        "with immediate effect.",
    )
    assert result.name == "Circulars"
    assert result.ranking[1].name == "Policies"  # mentions a policy, but is a circular


def test_no_evidence_gives_no_suggestion():
    result = classify(CATEGORIES, None, "Lorem ipsum dolor", "sit amet")
    assert result.category_id is None and result.confidence == 0.0


def test_weak_evidence_gives_low_confidence():
    result = classify(CATEGORIES, None, "", "please refer to the manual.")
    assert result.name == "Manuals"
    assert result.confidence < 0.6
