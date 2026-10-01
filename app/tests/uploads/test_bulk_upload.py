"""Bulk upload with version management, end to end through the real pipeline."""
import io
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from app.modules.categories.model import Category
from app.modules.policies.model import PolicyVersion
from app.tests.factories import login, make_tenant
from app.tests.flows import V3, V4, build, confirm_new_policy, process
from app.tests.pdfs import PolicySpec, Section
from app.tests.pipeline import drain

pytestmark = pytest.mark.integration


@pytest.fixture
def tenant(db):
    return make_tenant(db)


@pytest.fixture
def admin(client, tenant):
    return login(client, tenant.admin)


def category_id(db, tenant, slug="policies"):
    return str(db.scalar(select(Category.id).where(Category.organization_id == tenant.org.id, Category.slug == slug)))


def undated(version: str, ltv: str, extra: list[Section]) -> bytes:
    """A version of the same policy with no date anywhere in it."""
    spec = PolicySpec(header_lines=["Policy No: GL-7", f"Version: {version}", "Issued by: Retail Credit"])
    spec.title = "GOLD LOAN POLICY"
    spec.sections[3].paragraphs = [f"For loans above Rs. 75 lakh the LTV shall not exceed {ltv}."]
    spec.sections.extend(extra)
    return build(spec)


def run_batch(session, app, groups, files):
    """Create the plan, send every file, let the pipeline run; return the finished batch."""
    created = session.post("/uploads/batches", json={"groups": groups})
    assert created.status_code == 201, created.text
    batch = created.json()["data"]
    for group, group_files in zip(batch["groups"], files, strict=True):
        for item, content in zip(group["items"], group_files, strict=True):
            sent = session.post(f"/uploads/batches/{batch['id']}/items/{item['id']}/file",
                                files={"file": (item["original_filename"], io.BytesIO(content), "application/pdf")})
            assert sent.status_code == 200, sent.text
    drain(app)
    started = session.post(f"/uploads/batches/{batch['id']}/start")
    assert started.status_code == 200, started.text
    drain(app)
    return session.get(f"/uploads/batches/{batch['id']}").json()["data"]


def versions(db, policy_id):
    return db.scalars(select(PolicyVersion).where(PolicyVersion.policy_id == policy_id)
                      .order_by(PolicyVersion.effective_from)).all()


def test_new_policy_with_versions_is_registered_in_the_arranged_order(db, app, tenant, admin):
    batch = run_batch(admin, app, [{
        "new_policy_name": "Home Loan Credit Policy", "category_id": category_id(db, tenant),
        "items": [{"filename": "home-loan-v3.pdf"}, {"filename": "home-loan-v4.pdf"}],
    }], [[build(V3), build(V4)]])
    assert batch["status"] == "completed", batch
    group = batch["groups"][0]
    assert [i["status"] for i in group["items"]] == ["confirmed", "confirmed"]
    history = versions(db, group["policy_id"])
    assert [v.version_label for v in history] == ["3", "4"]
    # The documents state their dates, so those are used.
    assert [(v.effective_from, v.effective_date_source) for v in history] == [
        (date(2025, 1, 1), "detected"), (date(2026, 7, 1), "detected")]
    assert str(history[-1].document_id) == group["items"][-1]["document_id"]


def test_undated_versions_follow_the_arranged_order(db, app, tenant, admin):
    files = [
        undated("1", "75%", []),
        undated("2", "72%", [Section("7", "Top-up Loans", ["Top-up loans are allowed after 12 repayments."])]),
        undated("3", "70%", [Section("7", "Top-up Loans", ["Top-up loans are allowed after 12 repayments."]),
                             Section("8", "Part Prepayment", ["Part prepayment carries no charge for individuals."])]),
    ]
    batch = run_batch(admin, app, [{
        "new_policy_name": "Gold Loan Policy", "category_id": category_id(db, tenant),
        "items": [{"filename": "gold-1.pdf"}, {"filename": "gold-2.pdf"}, {"filename": "gold-3.pdf"}],
    }], [files])
    assert batch["status"] == "completed", batch
    history = versions(db, batch["groups"][0]["policy_id"])
    today = datetime.now(UTC).date()
    assert [v.version_label for v in history] == ["1", "2", "3"]
    assert [v.effective_date_source for v in history] == ["inferred", "inferred", "upload_date"]
    assert [v.effective_from for v in history] == [today - timedelta(days=2), today - timedelta(days=1), today]
    assert history[-1].effective_to is None and history[0].effective_to == history[1].effective_from


def test_versions_can_be_added_to_an_existing_policy(db, app, tenant, admin, client):
    document_id, analysis = process(client, app, tenant.admin, build(V3))
    policy_id = confirm_new_policy(admin, document_id, analysis).json()["data"]["policy_id"]
    drain(app)
    batch = run_batch(admin, app, [{"policy_id": policy_id, "items": [{"filename": "v4.pdf"}]}], [[build(V4)]])
    assert batch["status"] == "completed", batch
    assert [v.version_label for v in versions(db, policy_id)] == ["3", "4"]


def test_a_file_matching_another_policy_waits_for_review_then_completes(db, app, tenant, admin, client):
    document_id, analysis = process(client, app, tenant.admin, build(V3))
    policy_id = confirm_new_policy(admin, document_id, analysis).json()["data"]["policy_id"]
    drain(app)
    # Arranged as a *new* policy, but it is really v4 of the existing one.
    batch = run_batch(admin, app, [{
        "new_policy_name": "Another Home Loan Policy", "category_id": category_id(db, tenant),
        "items": [{"filename": "v4.pdf"}],
    }], [[build(V4)]])
    item = batch["groups"][0]["items"][0]
    assert batch["status"] == "attention" and item["status"] == "needs_review"
    assert "existing policy" in item["message"]
    # A person reviews it and files it under the right policy: the upload completes.
    reviewed = admin.post(f"/documents/{item['document_id']}/confirm", json={
        "action": "add_version", "policy_id": policy_id, "version": {"version_label": "4"}})
    assert reviewed.status_code == 200, reviewed.text
    after = admin.get(f"/uploads/batches/{batch['id']}").json()["data"]
    assert after["status"] == "completed" and after["groups"][0]["items"][0]["message"] == "Confirmed after review."


def test_a_file_added_to_the_wrong_policy_waits_for_review(db, app, tenant, admin, client):
    document_id, analysis = process(client, app, tenant.admin, build(V3))
    policy_id = confirm_new_policy(admin, document_id, analysis).json()["data"]["policy_id"]
    drain(app)
    # An unrelated policy exists; v4 is arranged under it but is really a version of the first.
    other = run_batch(admin, app, [{
        "new_policy_name": "Current Account Policy", "category_id": category_id(db, tenant),
        "items": [{"filename": "ca-1.pdf"}],
    }], [[undated("1", "60%", [Section("7", "Dormant Accounts", ["Dormant accounts are closed after two years."])])]])
    other_policy = other["groups"][0]["policy_id"]
    assert other_policy != policy_id
    before = len(versions(db, other_policy))

    batch = run_batch(admin, app, [{"policy_id": other_policy, "items": [{"filename": "v4.pdf"}]}], [[build(V4)]])
    item = batch["groups"][0]["items"][0]
    assert item["status"] == "needs_review", batch
    assert "Home Loan" in item["message"], item["message"]
    # Nothing was filed: the wrong policy's history is untouched until a person decides.
    assert len(versions(db, other_policy)) == before

    reviewed = admin.post(f"/documents/{item['document_id']}/confirm", json={
        "action": "add_version", "policy_id": policy_id, "version": {"version_label": "4"}})
    assert reviewed.status_code == 200, reviewed.text
    assert [v.version_label for v in versions(db, policy_id)] == ["3", "4"]


def test_a_file_matching_its_own_policy_is_confirmed_unattended(db, app, tenant, admin, client):
    document_id, analysis = process(client, app, tenant.admin, build(V3))
    policy_id = confirm_new_policy(admin, document_id, analysis).json()["data"]["policy_id"]
    drain(app)
    batch = run_batch(admin, app, [{"policy_id": policy_id, "items": [{"filename": "v4.pdf"}]}], [[build(V4)]])
    assert batch["status"] == "completed", batch
    assert batch["groups"][0]["items"][0]["status"] == "confirmed"


def test_a_file_that_cannot_be_accepted_does_not_stop_the_upload(db, app, tenant, admin):
    batch = run_batch(admin, app, [{
        "new_policy_name": "Home Loan Credit Policy", "category_id": category_id(db, tenant),
        "items": [{"filename": "notes.pdf"}, {"filename": "v3.pdf"}],
    }], [[b"this is not a pdf", build(V3)]])
    items = batch["groups"][0]["items"]
    assert items[0]["status"] == "failed" and items[1]["status"] == "confirmed"
    assert batch["status"] == "attention"


def test_bulk_upload_requires_policy_management(client, tenant):
    user = login(client, tenant.user_a1)
    response = user.post("/uploads/batches", json={"groups": [{"items": [{"filename": "x.pdf"}]}]})
    assert response.status_code == 403


def test_confirming_without_an_effective_date_uses_the_documents_own_date(db, app, tenant, admin, client):
    document_id, analysis = process(client, app, tenant.admin, build(V3))
    response = admin.post(f"/documents/{document_id}/confirm", json={
        "action": "create_policy",
        "policy": {"name": "Home Loan Credit Policy", "category_id": analysis["suggested_category_id"]},
        "version": {"version_label": "3"},
    })
    assert response.status_code == 200, response.text
    [version] = versions(db, response.json()["data"]["policy_id"])
    assert (version.effective_from, version.effective_date_source) == (date(2025, 1, 1), "detected")


def test_two_undated_versions_on_the_same_day_keep_their_order(db, app, tenant, admin, client):
    first, analysis = process(client, app, tenant.admin, undated("1", "75%", []))
    policy_id = admin.post(f"/documents/{first}/confirm", json={
        "action": "create_policy",
        "policy": {"name": "Gold Loan Policy", "category_id": analysis["suggested_category_id"]},
        "version": {},
    }).json()["data"]["policy_id"]
    second, _ = process(client, app, tenant.admin, undated("2", "70%", [
        Section("7", "Top-up Loans", ["Top-up loans are allowed after 12 repayments."])]))
    response = admin.post(f"/documents/{second}/confirm", json={
        "action": "add_version", "policy_id": policy_id, "version": {}})
    assert response.status_code == 200, response.text
    older, newer = versions(db, policy_id)
    today = datetime.now(UTC).date()
    # The earlier placeholder date made room; the newest upload is current.
    assert (older.effective_from, newer.effective_from) == (today - timedelta(days=1), today)
    assert older.effective_to == today and newer.effective_to is None


def suggest(session, content: bytes, filename="011123-scan.pdf"):
    response = session.post("/uploads/suggestions", files={"file": (filename, io.BytesIO(content), "application/pdf")})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_a_new_policy_is_named_from_the_document_before_upload(db, tenant, admin):
    suggestion = suggest(admin, build(V3))
    # The name the document states, not the file name.
    assert suggestion["readable"] and suggestion["name"] == "Home Loan Credit Policy"
    assert suggestion["version_label"] == "3" and suggestion["effective_date"] == "2025-01-01"
    assert suggestion["existing_policy"] is None and suggestion["identical_document"] is None


def test_the_suggestion_points_at_the_policy_a_new_version_belongs_to(db, app, tenant, admin, client):
    v3 = build(V3)
    document_id, analysis = process(client, app, tenant.admin, v3)
    policy_id = confirm_new_policy(admin, document_id, analysis).json()["data"]["policy_id"]
    drain(app)
    existing = suggest(admin, build(V4))["existing_policy"]
    assert existing["policy_id"] == policy_id and existing["name"] == "Home Loan Credit Policy"
    assert existing["current_version_label"] == "3" and existing["version_count"] == 1
    assert existing["can_add_version"] and "Policy number HL-2025-01 matches" in existing["reasons"]
    # The very same file is reported as already uploaded.
    identical = suggest(admin, v3)["identical_document"]
    assert identical["document_id"] == str(document_id) and identical["policy_id"] == policy_id


def test_an_unrelated_policy_is_not_suggested(db, app, tenant, admin, client):
    document_id, analysis = process(client, app, tenant.admin, build(V3))
    confirm_new_policy(admin, document_id, analysis)
    drain(app)
    spec = PolicySpec(header_lines=["Policy No: GL-7", "Version: 1", "Issued by: Retail Credit"])
    spec.title = "GOLD LOAN POLICY"
    assert suggest(admin, build(spec))["existing_policy"] is None


def test_an_unreadable_file_gets_no_suggestion(tenant, admin):
    suggestion = suggest(admin, b"%PDF-1.4 not really a pdf")
    assert not suggestion["readable"] and suggestion["name"] is None and suggestion["existing_policy"] is None


def bare_policy(db, tenant, name: str) -> str:
    """A policy with no number and no versions, known only by its name."""
    from app.modules.ingestion.analysis.metadata import normalize_title
    from app.modules.policies.model import Policy
    policy = Policy(organization_id=tenant.org.id, category_id=category_id(db, tenant), name=name,
                    normalized_name=normalize_title(name))
    db.add(policy)
    db.commit()
    return str(policy.id)


def test_a_file_meant_for_the_wrong_policy_points_at_the_right_one(db, app, tenant, admin, client):
    document_id, analysis = process(client, app, tenant.admin, build(V3))
    home_loan = confirm_new_policy(admin, document_id, analysis).json()["data"]["policy_id"]
    drain(app)
    wrong = bare_policy(db, tenant, "Record and Data Retention Policy")
    response = admin.post("/uploads/suggestions", data={"policy_id": wrong},
                          files={"file": ("v4.pdf", io.BytesIO(build(V4)), "application/pdf")})
    assert response.json()["data"]["existing_policy"]["policy_id"] == home_loan


def test_the_policy_a_file_is_meant_for_wins_when_it_fits_too(db, app, tenant, admin):
    spec = PolicySpec(header_lines=["Version: 2", "Issued by: Credit Department"])
    first = bare_policy(db, tenant, "Home Loan Credit Policy")
    second = bare_policy(db, tenant, "Home Loan Credit Policy for Branches")
    for meant in (first, second):
        response = admin.post("/uploads/suggestions", data={"policy_id": meant},
                              files={"file": ("v2.pdf", io.BytesIO(build(spec)), "application/pdf")})
        assert response.json()["data"]["existing_policy"]["policy_id"] == meant


def unclassifiable() -> bytes:
    """A document no category's keywords describe."""
    return build(PolicySpec(
        title="PETS 2.0 FEATURE REQUIREMENTS",
        header_lines=["Owner: Digital Banking", "Release: 2.0"],
        sections=[
            Section("1", "Overview", ["Customers can buy pet food and accessories in the mobile app."]),
            Section("2", "Variants", ["Each item lists its weight and dose variants with separate prices."]),
        ],
    ))


def test_a_file_whose_category_is_not_identified_is_filed_under_policies(db, app, tenant, admin):
    batch = run_batch(admin, app, [{
        "new_policy_name": "Pets 2.0 Feature Requirements",
        "items": [{"filename": "pets.pdf"}],
    }], [[unclassifiable()]])
    assert batch["status"] == "completed", batch
    group = batch["groups"][0]
    assert group["items"][0]["status"] == "confirmed"
    from app.modules.policies.model import Policy
    assert str(db.get(Policy, group["policy_id"]).category_id) == category_id(db, tenant)


def test_an_unidentified_category_is_suggested_as_policies_for_review(db, app, client, tenant):
    _document_id, analysis = process(client, app, tenant.admin, unclassifiable())
    assert analysis["suggested_category_id"] == category_id(db, tenant)
    assert "category" not in analysis["missing_fields"]
