"""Categories as the home of policies: types, counts, contents and arrangement."""

import io

import pytest
from sqlalchemy import select

from app.modules.categories.model import Category
from app.tests.factories import login, make_tenant
from app.tests.flows import V3, V4, build, confirm_new_policy, confirm_new_version, home_loan_spec, process
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


@pytest.fixture
def home_loan(client, app, tenant, admin):
    """Home Loan Credit Policy with v3 (2025) and v4 (2026-07)."""
    document_id, analysis = process(client, app, tenant.admin, build(V3))
    policy_id = confirm_new_policy(admin, document_id, analysis).json()["data"]["policy_id"]
    document_id, analysis = process(client, app, tenant.admin, build(V4))
    assert confirm_new_version(admin, document_id, analysis, policy_id).status_code == 200
    drain(app)
    return policy_id


def test_create_category_with_type_and_generated_slug(admin):
    created = admin.post("/categories", json={
        "name": "  Home   Loan Policies ", "category_type": "policy",
        "description": "Guidelines for home loan eligibility, approval and processing",
    })
    assert created.status_code == 201, created.text
    data = created.json()["data"]
    assert (data["name"], data["slug"], data["category_type"]) == ("Home Loan Policies", "home-loan-policies", "policy")
    assert data["authority_rank"] == 80  # from the type
    assert data["is_active"] is True


def test_type_is_required(admin):
    response = admin.post("/categories", json={"name": "Treasury"})
    assert response.status_code == 422


def test_duplicate_names_are_refused_ignoring_case(admin):
    assert admin.post("/categories", json={"name": "Treasury", "category_type": "other"}).status_code == 201
    duplicate = admin.post("/categories", json={"name": "treasury", "category_type": "manual"})
    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["error"]["message"]
    # A seeded category name is taken too.
    assert admin.post("/categories", json={"name": "CIRCULARS", "category_type": "circular"}).status_code == 409


def test_update_type_status_and_clear_description(admin):
    created = admin.post("/categories", json={"name": "Forms Library", "category_type": "form", "description": "x"}).json()["data"]
    updated = admin.patch(f"/categories/{created['id']}", json={
        "category_type": "manual", "is_active": False, "description": None,
    })
    assert updated.status_code == 200, updated.text
    data = updated.json()["data"]
    assert (data["category_type"], data["is_active"], data["description"]) == ("manual", False, None)
    rename_clash = admin.patch(f"/categories/{created['id']}", json={"name": "Policies"})
    assert rename_clash.status_code == 409


def test_only_admins_manage_categories(client, tenant):
    manager = login(client, tenant.manager_a)
    assert manager.post("/categories", json={"name": "Branch Stuff", "category_type": "other"}).status_code == 403
    assert manager.get("/categories/overview").status_code == 200


def test_overview_counts_policies_and_tracks_activity(db, admin, tenant, home_loan):
    overview = {c["slug"]: c for c in admin.get("/categories/overview").json()["data"]}
    policies = overview["policies"]
    assert policies["document_count"] == 1 and policies["category_type"] == "policy"
    assert overview["faqs"]["document_count"] == 0
    assert policies["last_updated"] >= overview["faqs"]["last_updated"]
    single = admin.get(f"/categories/{category_id(db, tenant)}").json()["data"]
    assert single["document_count"] == 1


def test_documents_list_versions_with_their_files(db, admin, tenant, home_loan):
    page = admin.get(f"/categories/{category_id(db, tenant)}/documents").json()["data"]
    assert page["total"] == 1
    [policy] = page["items"]
    assert policy["name"] == "Home Loan Credit Policy" and policy["version_count"] == 2
    labels = [v["version_label"] for v in policy["versions"]]
    assert labels == ["4", "3"]  # newest first
    newest = policy["versions"][0]
    assert newest["timeline_state"] == "current" and policy["current_version_id"] == newest["id"]
    assert newest["filename"].endswith(".pdf") and newest["size_bytes"] > 0
    assert newest["uploaded_by_name"] == tenant.admin.full_name

    historical = admin.get(f"/categories/{category_id(db, tenant)}/documents?version_state=historical").json()["data"]
    assert [v["version_label"] for v in historical["items"][0]["versions"]] == ["3"]
    assert admin.get(f"/categories/{category_id(db, tenant)}/documents?search=home%20loan").json()["data"]["total"] == 1
    assert admin.get(f"/categories/{category_id(db, tenant)}/documents?search=50%25").json()["data"]["total"] == 0
    assert admin.get(f"/categories/{category_id(db, tenant)}/documents?status=archived").json()["data"]["total"] == 0


def test_pending_uploads_are_listed_until_confirmed(client, app, db, admin, tenant):
    target = category_id(db, tenant, "circulars")
    document_id, _ = process(client, app, tenant.admin, build(V3), category_id=target)
    pending = admin.get(f"/categories/{target}/pending").json()["data"]
    assert [p["document_id"] for p in pending] == [str(document_id)]
    assert pending[0]["status"] == "awaiting_confirmation"


def test_policies_can_be_arranged_within_a_category(client, app, db, admin, tenant, home_loan):
    gold = home_loan_spec("1", "01/01/2024")
    gold.title = "GOLD LOAN POLICY"
    gold.header_lines[0] = "Policy No: GL-2024-01"
    document_id, analysis = process(client, app, tenant.admin, build(gold))
    analysis["suggested_category_id"] = category_id(db, tenant)
    gold_id = confirm_new_policy(admin, document_id, analysis, policy={
        "name": "Gold Loan Policy", "category_id": category_id(db, tenant), "policy_number": "GL-2024-01",
    }).json()["data"]["policy_id"]

    url = f"/categories/{category_id(db, tenant)}/documents"
    names = lambda: [p["name"] for p in admin.get(url).json()["data"]["items"]]
    assert names() == ["Gold Loan Policy", "Home Loan Credit Policy"]  # unarranged: newest first
    moved = admin.post(f"/policies/{gold_id}/move", json={"after_id": home_loan})
    assert moved.status_code == 200, moved.text
    assert names() == ["Home Loan Credit Policy", "Gold Loan Policy"]
    assert admin.post(f"/policies/{gold_id}/move", json={}).status_code == 200  # to the top
    assert names() == ["Gold Loan Policy", "Home Loan Credit Policy"]
    both = admin.post(f"/policies/{gold_id}/move", json={"after_id": home_loan, "before_id": home_loan})
    assert both.status_code == 422


def test_withdrawn_version_can_be_restored(admin, home_loan):
    detail = admin.get(f"/policies/{home_loan}").json()["data"]
    v3, v4 = detail["versions"]
    withdrawn = admin.post(f"/policies/{home_loan}/versions/{v4['id']}/withdraw", json={"reason": "Issued in error"})
    assert withdrawn.status_code == 200
    after = admin.get(f"/policies/{home_loan}").json()["data"]["versions"]
    assert after[0]["effective_to"] is None  # v3 covers the gap

    restored = admin.post(f"/policies/{home_loan}/versions/{v4['id']}/restore")
    assert restored.status_code == 200, restored.text
    assert restored.json()["data"]["status"] == "active"
    v3, v4 = admin.get(f"/policies/{home_loan}").json()["data"]["versions"]
    assert v3["effective_to"] == "2026-07-01" and v3["superseded_by_version_id"] == v4["id"]
    assert (v4["effective_to"], v4["supersedes_version_id"], v4["withdrawn_reason"]) == (None, v3["id"], None)
    assert admin.post(f"/policies/{home_loan}/versions/{v4['id']}/restore").status_code == 422


def test_cancelled_upload_item_does_not_block_its_group(db, admin, tenant):
    batch = admin.post("/uploads/batches", json={"groups": [{
        "category_id": category_id(db, tenant), "items": [{"filename": "a.pdf"}, {"filename": "b.pdf"}],
    }]}).json()["data"]
    first, second = batch["groups"][0]["items"]
    cancelled = admin.post(f"/uploads/batches/{batch['id']}/items/{first['id']}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["data"]["status"] == "cancelled"
    admin.post(f"/uploads/batches/{batch['id']}/items/{second['id']}/cancel")
    finished = admin.get(f"/uploads/batches/{batch['id']}").json()["data"]
    assert finished["status"] == "completed" and finished["counts"]["cancelled"] == 2
    late = admin.post(f"/uploads/batches/{batch['id']}/items/{first['id']}/file",
                      files={"file": ("a.pdf", io.BytesIO(build(V3)), "application/pdf")})
    assert late.status_code == 409
