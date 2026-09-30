from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.infrastructure.queue.models import QueueJob
from app.modules.documents.model import Document
from app.modules.policies.model import PolicyVersion
from app.tests.factories import login, make_tenant
from app.tests.flows import V3, V4, build, confirm_new_policy, confirm_new_version, home_loan_spec, process
from app.tests.pdfs import PolicySpec, Section

pytestmark = pytest.mark.integration


@pytest.fixture
def tenant(db):
    return make_tenant(db)


@pytest.fixture
def admin(client, tenant):
    return login(client, tenant.admin)


@pytest.fixture
def policy_v3(client, app, tenant, admin):
    document_id, analysis = process(client, app, tenant.admin, build(V3))
    response = confirm_new_policy(admin, document_id, analysis)
    assert response.status_code == 200, response.text
    policy_id = response.json()["data"]["policy_id"]
    return policy_id, document_id


def test_confirming_a_new_policy(db, app, admin, policy_v3):
    policy_id, document_id = policy_v3
    document = db.get(Document, document_id)
    assert document.status == "indexing"
    assert document.title == "Home Loan Credit Policy"
    assert db.scalar(select(QueueJob).where(QueueJob.task_name == "ingestion.chunk"))

    detail = admin.get(f"/policies/{policy_id}").json()["data"]
    assert detail["name"] == "Home Loan Credit Policy" and detail["policy_number"] == "HL-2025-01"
    [version] = detail["versions"]
    assert (version["version_label"], version["effective_from"], version["timeline_state"]) == ("3", "2025-01-01", "current")
    analysis = admin.get(f"/documents/{document_id}/analysis").json()["data"]
    assert analysis["review_status"] == "confirmed"
    assert admin.post(f"/documents/{document_id}/confirm", json={"action": "reject"}).status_code == 409


def test_new_version_joins_the_timeline_with_a_deterministic_diff(client, db, app, tenant, admin, policy_v3):
    policy_id, v3_document = policy_v3
    document_id, analysis = process(client, app, tenant.admin, build(V4))
    assert analysis["decision"] == "EXISTING_POLICY_NEW_VERSION"
    response = confirm_new_version(admin, document_id, analysis, policy_id)
    assert response.status_code == 200, response.text

    detail = admin.get(f"/policies/{policy_id}").json()["data"]
    v3, v4 = detail["versions"]
    assert v3["effective_to"] == "2026-07-01" and v3["superseded_by_version_id"] == v4["id"]
    assert v4["supersedes_version_id"] == v3["id"] and v4["effective_to"] is None
    assert detail["version_count"] == 2

    version = admin.get(f"/policies/{policy_id}/versions/{v4['id']}").json()["data"]
    summary = version["change_summary"]["summary_lines"]
    assert "Changed: 5.2 LTV for High Value Loans (75% → 70%)" in summary
    assert "Added: 7 NRI Eligibility" in summary

    # Currency follows effective dates, not upload order.
    as_of = lambda d: admin.get(f"/policies?as_of={d}").json()["data"]["items"][0]["current_version"]["version_label"]
    assert as_of("2025-06-01") == "3"
    assert as_of("2026-08-01") == "4"

    comparison = admin.get(f"/policies/{policy_id}/compare?base={v3['id']}&target={v4['id']}").json()["data"]
    ltv = next(m for m in comparison["modified"] if m["new"]["number"] == "5.2")
    assert ltv["old"]["content"].endswith("75%.") and ltv["new"]["content"].endswith("70%.")


def test_historical_version_is_inserted_in_the_past(client, app, tenant, admin, policy_v3):
    policy_id, _ = policy_v3
    v2 = home_loan_spec("2", "01/04/2023", ltv="80%")
    document_id, analysis = process(client, app, tenant.admin, build(v2))
    assert "HISTORICAL_VERSION" in analysis["detected"].get("notes", [])
    assert confirm_new_version(admin, document_id, analysis, policy_id).status_code == 200

    versions = admin.get(f"/policies/{policy_id}").json()["data"]["versions"]
    assert [(v["version_label"], v["effective_from"], v["effective_to"]) for v in versions] == [
        ("2", "2023-04-01", "2025-01-01"),
        ("3", "2025-01-01", None),
    ]
    assert [v["timeline_state"] for v in versions] == ["historical", "current"]


def test_version_conflict_requires_explicit_resolution(client, db, app, tenant, admin, policy_v3):
    policy_id, _ = policy_v3
    clash = home_loan_spec("3", "01/03/2025", ltv="72%")
    document_id, analysis = process(client, app, tenant.admin, build(clash))
    assert analysis["decision"] == "VERSION_CONFLICT"

    no_reason = confirm_new_version(admin, document_id, analysis, policy_id)
    assert no_reason.json()["error"]["code"] == "OVERRIDE_REASON_REQUIRED"
    conflict = confirm_new_version(admin, document_id, analysis, policy_id, reason="Corrected reissue of v3")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "VERSION_CONFLICT"
    assert "save_as_new_revision" in conflict.json()["error"]["details"]["options"]

    resolved = confirm_new_version(
        admin, document_id, analysis, policy_id,
        reason="Corrected reissue of v3", conflict_resolution="save_as_new_revision",
    )
    assert resolved.status_code == 200, resolved.text
    labels = [(v.version_label, v.revision_number) for v in db.scalars(
        select(PolicyVersion).where(PolicyVersion.policy_id == policy_id).order_by(PolicyVersion.effective_from))]
    assert labels == [("3", 0), ("3", 1)]  # nothing overwritten


def test_same_effective_date_is_rejected(client, app, tenant, admin, policy_v3):
    policy_id, _ = policy_v3
    same_day = home_loan_spec("4", "01/01/2025", ltv="60%")
    document_id, analysis = process(client, app, tenant.admin, build(same_day))
    response = confirm_new_version(admin, document_id, analysis, policy_id, reason="testing same day")
    assert response.status_code == 409
    assert response.json()["error"]["details"]["type"] == "EFFECTIVE_DATE_EXISTS"


def test_database_forbids_overlapping_active_versions(db, policy_v3):
    policy_id, _ = policy_v3
    existing = db.scalar(select(PolicyVersion).where(PolicyVersion.policy_id == policy_id))
    overlapping = PolicyVersion(
        organization_id=existing.organization_id, policy_id=existing.policy_id,
        document_id=existing.document_id, version_number=99, version_label="x",
        effective_from=date(2025, 6, 1),
    )
    db.add(overlapping)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_withdrawing_a_version_restores_its_predecessor(client, app, tenant, admin, policy_v3):
    policy_id, _ = policy_v3
    document_id, analysis = process(client, app, tenant.admin, build(V4))
    confirm_new_version(admin, document_id, analysis, policy_id)
    v3, v4 = admin.get(f"/policies/{policy_id}").json()["data"]["versions"]

    response = admin.post(f"/policies/{policy_id}/versions/{v4['id']}/withdraw", json={"reason": "Issued in error"})
    assert response.status_code == 200
    v3, v4 = admin.get(f"/policies/{policy_id}").json()["data"]["versions"]
    assert v4["status"] == "withdrawn" and v4["timeline_state"] == "withdrawn"
    assert v3["effective_to"] is None and v3["timeline_state"] == "current"


def test_circular_is_confirmed_with_an_amends_relationship(client, app, tenant, admin, policy_v3):
    policy_id, _ = policy_v3
    circular = PolicySpec(
        title="CIRCULAR: REVISION OF LTV NORMS",
        header_lines=["Circular No: CRD/2026/45", "Effective Date: 01/08/2026"],
        sections=[Section("1", "Revision", [
            "Clause 5.2 of the Home Loan Credit Policy is hereby amended. The LTV for loans above "
            "Rs. 75 lakh shall be 65%.",
        ])],
    )
    document_id, analysis = process(client, app, tenant.admin, build(circular))
    target = analysis["amendment_targets"][0]
    response = confirm_new_policy(admin, document_id, analysis, relationships=[{
        "relation_type": target["relation_type"], "target_policy_id": target["policy_id"],
        "target_version_id": target["version_id"], "clauses": target["clauses"], "evidence": target["evidence"],
    }], policy={
        "name": "Circular CRD/2026/45 - Revision of LTV Norms",
        "category_id": analysis["suggested_category_id"],
        "document_number": "CRD/2026/45",
    })
    assert response.status_code == 200, response.text

    incoming = admin.get(f"/policies/{policy_id}").json()["data"]["incoming_relationships"]
    assert [(r["relation_type"], r["clauses"]) for r in incoming] == [("AMENDS", ["5.2"])]
    assert incoming[0]["source_title"] == "Circular CRD/2026/45 - Revision of LTV Norms"


def test_reject_and_permissions(client, app, tenant, admin, policy_v3):
    document_id, analysis = process(client, app, tenant.admin, build(V4))
    user = login(client, tenant.user_a1)
    assert confirm_new_version(user, document_id, analysis, policy_v3[0]).status_code == 403

    response = admin.post(f"/documents/{document_id}/confirm", json={"action": "reject", "reason": "Wrong file"})
    assert response.json()["data"]["status"] == "rejected"


def test_policy_number_must_be_unique(client, app, tenant, admin, policy_v3):
    other = PolicySpec(title="VEHICLE LOAN POLICY", header_lines=["Policy No: HL-2025-01", "Effective Date: 01/01/2026"])
    document_id, analysis = process(client, app, tenant.admin, build(other))
    response = confirm_new_policy(admin, document_id, analysis, reason="different product")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "POLICY_NUMBER_EXISTS"


def test_branch_scoped_policies_are_isolated(client, app, tenant):
    manager_b = login(client, tenant.manager_b)
    document_id, analysis = process(client, app, tenant.manager_b, build(V3))
    policy_id = confirm_new_policy(manager_b, document_id, analysis).json()["data"]["policy_id"]

    user_a = login(client, tenant.user_a1)
    assert user_a.get(f"/policies/{policy_id}").status_code == 404
    assert user_a.get("/policies").json()["data"]["total"] == 0
    # A branch B User sees it once it is assigned to them, not before.
    user_b = login(client, tenant.user_b1)
    assert user_b.get(f"/policies/{policy_id}").status_code == 404
    assert manager_b.post(f"/policies/{policy_id}/assignments", json={"user_ids": [str(tenant.user_b1.id)]}).status_code == 200
    assert user_b.get(f"/policies/{policy_id}").status_code == 200
    # A Branch Manager cannot hand it to another branch's User.
    assert manager_b.post(f"/policies/{policy_id}/assignments", json={"user_ids": [str(tenant.user_a1.id)]}).status_code == 404
