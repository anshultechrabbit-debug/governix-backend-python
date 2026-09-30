import io
import uuid
from datetime import date

import pytest
from sqlalchemy import select

from app.modules.audit.model import AuditEvent
from app.modules.documents.model import Document
from app.tests.factories import login, make_policy_from_document, make_tenant
from app.tests.pdfs import PolicySpec, Section, build_policy_pdf
from app.tests.pipeline import drain

pytestmark = pytest.mark.integration


@pytest.fixture
def tenant(db):
    return make_tenant(db)


def process(client, app, user, pdf: bytes, **form):
    session = login(client, user)
    response = session.post(
        "/documents",
        files={"file": (f"{uuid.uuid4().hex}.pdf", io.BytesIO(pdf), "application/pdf")},
        data={k: str(v) for k, v in form.items()},
    )
    assert response.status_code == 201, response.text
    document_id = response.json()["data"]["id"]
    drain(app)
    analysis = session.get(f"/documents/{document_id}/analysis")
    assert analysis.status_code == 200, analysis.text
    return uuid.UUID(document_id), analysis.json()["data"]


def v3_spec(**overrides):
    base = dict(header_lines=[
        "Policy No: HL-2025-01", "Version: 3", "Issued by: Credit Department", "Effective Date: 01/01/2025",
    ])
    return PolicySpec(**{**base, **overrides})


def v4_spec(**overrides):
    spec = v3_spec(header_lines=[
        "Policy No: HL-2025-01", "Version: 4", "Issued by: Credit Department", "Effective Date: 01/07/2026",
    ])
    spec.sections[3].paragraphs = ["For loans above Rs. 75 lakh the LTV shall not exceed 70%."]
    spec.sections.append(Section("7", "NRI Eligibility", ["Non-resident Indians may apply with a co-applicant."]))
    for key, value in overrides.items():
        setattr(spec, key, value)
    return spec


def test_new_policy_is_identified_from_content_not_filename(client, db, app, tenant):
    document_id, analysis = process(client, app, tenant.admin, build_policy_pdf(v3_spec()))
    assert analysis["decision"] == "NEW_POLICY"
    assert analysis["suggested_name"] == "Home Loan Credit Policy"
    assert analysis["name_confidence"] >= 0.8
    assert analysis["suggested_category_name"] == "Policies"
    assert analysis["category_confidence"] >= 0.8
    detected = analysis["detected"]
    assert detected["policy_number"]["value"] == "HL-2025-01"
    assert detected["version_label"]["value"] == "3"
    assert detected["effective_date"] == "2025-01-01"
    assert analysis["suggested_initial_version"]["version_label"] == "3"
    assert analysis["review_status"] == "pending"

    document = db.get(Document, document_id)
    assert document.status == "awaiting_confirmation"
    assert document.title is None  # suggestions are not applied until confirmed
    progress = login(client, tenant.admin).get(f"/documents/{document_id}/progress").json()["data"]
    stages = {s["stage"]: s["status"] for s in progress["stages"]}
    assert stages["analysis"] == "completed" and stages["confirmation"] == "waiting"
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "document.analyzed"))


@pytest.fixture
def home_loan_v3(client, db, app, tenant):
    document_id, _ = process(client, app, tenant.admin, build_policy_pdf(v3_spec()))
    return make_policy_from_document(
        db, document_id, name="Home Loan Credit Policy", policy_number="HL-2025-01",
        label="3", effective_from=date(2025, 1, 1),
    )


def test_new_version_of_existing_policy(client, app, tenant, home_loan_v3):
    policy, version = home_loan_v3
    _, analysis = process(client, app, tenant.admin, build_policy_pdf(v4_spec()))
    assert analysis["decision"] == "EXISTING_POLICY_NEW_VERSION"
    assert analysis["confidence"] >= 0.75
    assert analysis["matched_policy"]["policy_id"] == str(policy.id)
    assert analysis["matched_policy"]["latest_version"]["label"] == "3"
    assert analysis["detected"]["version_label"]["value"] == "4"
    matched = {s["signal"] for s in analysis["signals"] if s["matched"]}
    assert {"policy_number", "title", "issuer", "category"} <= matched


def test_same_version_number_with_different_content_is_a_conflict(client, app, tenant, home_loan_v3):
    spec = v4_spec(header_lines=[
        "Policy No: HL-2025-01", "Version: 3", "Issued by: Credit Department", "Effective Date: 01/03/2025",
    ])
    _, analysis = process(client, app, tenant.admin, build_policy_pdf(spec))
    assert analysis["decision"] == "VERSION_CONFLICT"
    assert analysis["conflict"]["type"] == "VERSION_LABEL_EXISTS"
    assert analysis["conflict"]["existing_version"]["label"] == "3"


def test_same_text_in_a_different_file_is_a_content_duplicate(client, db, app, tenant, home_loan_v3):
    import pymupdf

    original = build_policy_pdf(v3_spec())
    doc = pymupdf.open(stream=original, filetype="pdf")
    doc.set_metadata({"title": "Re-exported copy", "author": "someone else"})
    re_exported = doc.tobytes(garbage=4, deflate=True)
    assert re_exported != original

    _, analysis = process(client, app, tenant.admin, re_exported)
    assert analysis["decision"] == "CONTENT_DUPLICATE"
    assert analysis["conflict"]["match"] == "text"
    assert analysis["duplicate_of_document_id"] == str(home_loan_v3[1].document_id)


def test_near_identical_rescan_is_a_semantic_duplicate(client, app, tenant, home_loan_v3):
    spec = v3_spec()
    extra = [f"Clause {i}: the branch shall verify income documents and property papers carefully." for i in range(30)]
    spec.sections[0].paragraphs = spec.sections[0].paragraphs + extra
    process(client, app, tenant.admin, build_policy_pdf(spec))
    spec.sections[0].paragraphs[-1] = spec.sections[0].paragraphs[-1].replace("carefully", "diligently")
    _, analysis = process(client, app, tenant.admin, build_policy_pdf(spec), allow_duplicate=False)
    assert analysis["decision"] == "CONTENT_DUPLICATE"
    assert analysis["conflict"]["match"] == "near_duplicate"
    assert analysis["confidence"] > 0.9


def test_circular_amending_a_policy_is_an_amendment(client, app, tenant, home_loan_v3):
    policy, version = home_loan_v3
    circular = PolicySpec(
        title="CIRCULAR: REVISION OF LTV NORMS",
        header_lines=["Circular No: CRD/2026/45", "Date: 15/06/2026", "To: All Branches"],
        sections=[Section("1", "Revision", [
            "All branches are hereby informed that Clause 5.2 of the Home Loan Credit Policy is hereby "
            "amended with effect from 01/07/2026. The LTV for loans above Rs. 75 lakh shall be 70%.",
        ])],
    )
    _, analysis = process(client, app, tenant.admin, build_policy_pdf(circular))
    assert analysis["decision"] == "EXISTING_POLICY_AMENDMENT"
    assert analysis["suggested_category_name"] == "Circulars"
    target = analysis["amendment_targets"][0]
    assert target["policy_id"] == str(policy.id)
    assert target["relation_type"] == "AMENDS"
    assert target["clauses"] == ["5.2"]
    assert target["version_id"] == str(version.id)


def test_matches_in_inaccessible_scope_are_redacted(client, db, app, tenant):
    # Branch B's policy must not leak to a branch A uploader through the match suggestion.
    document_id, _ = process(client, app, tenant.manager_b, build_policy_pdf(v3_spec()))
    make_policy_from_document(
        db, document_id, name="Home Loan Credit Policy", policy_number="HL-2025-01",
        label="3", effective_from=date(2025, 1, 1),
    )
    _, analysis = process(client, app, tenant.manager_a, build_policy_pdf(v4_spec()))
    assert analysis["matched_policy"]["restricted"] is True
    assert "name" not in analysis["matched_policy"]
    assert analysis["signals"] == []
    assert all(c.get("restricted") for c in analysis["candidates"])
