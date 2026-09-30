import uuid
from datetime import date

from app.modules.ingestion.analysis.matching import (
    CandidateFacts,
    UploadFacts,
    VersionFacts,
    analyze_version,
    decide,
)
from app.modules.ingestion.model import Decision

CATEGORY = uuid.uuid4()
SECTIONS = frozenset({"1 purpose", "2 eligibility", "5 loan to value", "6 interest rate"})


def version(label, effective, content_hash="old", revision=0):
    return VersionFacts(uuid.uuid4(), label, revision, effective, None, content_hash, uuid.uuid4())


def candidate(**overrides):
    base = dict(
        policy_id=uuid.uuid4(), name="Home Loan Credit Policy", normalized_name="home loan credit policy",
        policy_number="HL-2025-01", document_number=None, issuer="Credit Department",
        issuing_department="Credit Department", category_id=CATEGORY, title_similarity=1.0,
        versions=[version("3", date(2025, 1, 1))], latest_simhash=0b1010, latest_section_keys=SECTIONS,
    )
    return CandidateFacts(**{**base, **overrides})


def upload(**overrides):
    base = dict(
        normalized_title="home loan credit policy", policy_number="HL-2025-01", document_number=None,
        issuer="Credit Department", department="Credit Department", category_id=CATEGORY,
        effective_date=date(2026, 7, 1), version_label="4", revision=None, content_hash="new",
        simhash=0b1011, section_keys=SECTIONS | {"7 nri eligibility"},
    )
    return UploadFacts(**{**base, **overrides})


def run(u, candidates, amendments=False):
    return decide(u, candidates, high=0.75, low=0.45, has_amendment_targets=amendments)


def test_new_version_of_existing_policy():
    result, scored = run(upload(), [candidate()])
    assert result.decision is Decision.EXISTING_POLICY_NEW_VERSION
    assert result.confidence >= 0.9
    matched = {s.signal for s in scored[0].signals if s.matched}
    assert {"policy_number", "title", "issuer", "category", "content", "structure"} <= matched


def test_same_label_different_content_is_a_conflict():
    result, _ = run(upload(version_label="3"), [candidate()])
    assert result.decision is Decision.VERSION_CONFLICT
    assert result.conflict["type"] == "VERSION_LABEL_EXISTS"


def test_same_effective_date_is_a_conflict():
    result, _ = run(upload(effective_date=date(2025, 1, 1)), [candidate()])
    assert result.conflict["type"] == "EFFECTIVE_DATE_EXISTS"


def test_version_label_is_not_trusted_blindly():
    # Claims v4 but is effective before v3 -> suspicious ordering.
    result, _ = run(upload(effective_date=date(2024, 6, 1)), [candidate()])
    assert result.decision is Decision.VERSION_CONFLICT
    assert result.conflict["type"] == "VERSION_ORDER_MISMATCH"


def test_older_version_uploaded_later_is_historical():
    result, _ = run(upload(version_label="2", effective_date=date(2024, 1, 1)), [candidate()])
    assert result.decision is Decision.EXISTING_POLICY_NEW_VERSION
    assert "HISTORICAL_VERSION" in result.notes


def test_identical_content_to_existing_version_is_duplicate():
    result, _ = run(upload(content_hash="old"), [candidate()])
    assert result.decision is Decision.CONTENT_DUPLICATE


def test_different_policy_number_prevents_merge():
    result, _ = run(upload(policy_number="VL-2026-09"), [candidate()])
    assert result.decision in (Decision.NEW_POLICY, Decision.POSSIBLE_MATCH_REQUIRES_REVIEW)


def test_similar_title_without_identifiers_needs_review():
    u = upload(policy_number=None, normalized_title="home loan policy")
    result, _ = run(u, [candidate(policy_number=None, title_similarity=0.7)])
    assert result.decision is Decision.POSSIBLE_MATCH_REQUIRES_REVIEW


def test_no_candidates_is_new_policy():
    result, _ = run(upload(), [])
    assert result.decision is Decision.NEW_POLICY and result.confidence == 1.0


def test_amendment_detected():
    circular = upload(policy_number=None, normalized_title="revision of ltv norms", version_label=None)
    result, _ = run(circular, [candidate(title_similarity=0.3)], amendments=True)
    assert result.decision is Decision.EXISTING_POLICY_AMENDMENT


def test_missing_effective_date_is_flagged():
    _, _, notes = analyze_version(upload(effective_date=None, version_label=None), candidate())
    assert "EFFECTIVE_DATE_MISSING" in notes
