"""End-to-end helpers: upload -> pipeline -> analysis -> confirm."""

import io
import uuid

from app.tests.factories import login
from app.tests.pdfs import PolicySpec, Section, build_policy_pdf
from app.tests.pipeline import drain


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


def home_loan_spec(version: str, effective: str, ltv: str = "75%", extra_sections=()) -> PolicySpec:
    spec = PolicySpec(header_lines=[
        "Policy No: HL-2025-01", f"Version: {version}", "Issued by: Credit Department",
        f"Effective Date: {effective}",
    ])
    spec.sections[3].paragraphs = [f"For loans above Rs. 75 lakh the LTV shall not exceed {ltv}."]
    spec.sections.extend(extra_sections)
    return spec


V3 = home_loan_spec("3", "01/01/2025")
V4 = home_loan_spec("4", "01/07/2026", ltv="70%", extra_sections=[
    Section("7", "NRI Eligibility", ["Non-resident Indians may apply with a resident co-applicant."]),
])


def confirm_new_policy(session, document_id, analysis, **overrides):
    body = {
        "action": "create_policy",
        "policy": {
            "name": analysis["suggested_name"],
            "category_id": analysis["suggested_category_id"],
            "policy_number": analysis["detected"]["policy_number"]["value"],
            "issuer": analysis["detected"]["issuer"]["value"],
        },
        "version": {
            "version_label": analysis["suggested_initial_version"]["version_label"],
            "effective_from": analysis["suggested_initial_version"]["effective_from"],
        },
        **overrides,
    }
    return session.post(f"/documents/{document_id}/confirm", json=body)


def confirm_new_version(session, document_id, analysis, policy_id, **overrides):
    body = {
        "action": "add_version",
        "policy_id": str(policy_id),
        "version": {
            "version_label": analysis["detected"]["version_label"]["value"],
            "effective_from": analysis["detected"]["effective_date"],
        },
        **overrides,
    }
    return session.post(f"/documents/{document_id}/confirm", json=body)


def build(spec: PolicySpec) -> bytes:
    return build_policy_pdf(spec)
