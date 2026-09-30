"""The policy lifecycle of the platform flow: create, assign, reorder, move, summarise, notify."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.modules.audit.model import AuditEvent
from app.modules.categories.model import Category
from app.modules.documents.model import Document
from app.modules.notifications.model import Notification
from app.modules.policies.model import PolicyVersion
from app.modules.search.model import Chunk
from app.tests.factories import login, make_tenant
from app.tests.flows import V3, V4, build, confirm_new_policy, confirm_new_version, process
from app.tests.pdfs import PolicySpec, Section
from app.tests.pipeline import drain

pytestmark = pytest.mark.integration

NO_LABEL = {"version_label": None, "effective_from": None}


@pytest.fixture
def tenant(db):
    return make_tenant(db)


@pytest.fixture
def admin(client, tenant):
    return login(client, tenant.admin)


def category_id(db, tenant, slug="policies"):
    return str(db.scalar(select(Category.id).where(Category.organization_id == tenant.org.id, Category.slug == slug)))


def undated(ltv: str, extra: list[Section] = ()) -> bytes:
    """abc.pdf / xyz.pdf of the spec: same policy, no version label, no date."""
    spec = PolicySpec(header_lines=["Policy No: GL-7", "Issued by: Retail Credit"])
    spec.title = "GOLD LOAN POLICY"
    spec.sections[3].paragraphs = [f"For loans above Rs. 75 lakh the LTV shall not exceed {ltv}."]
    spec.sections.extend(extra)
    return build(spec)


@pytest.fixture
def gold(client, app, tenant, admin):
    """Two unlabelled, undated versions registered in upload order: "1" (abc) then "2" (xyz, latest)."""
    abc, analysis = process(client, app, tenant.admin, undated("70%"))
    policy_id = confirm_new_policy(admin, abc, analysis, version=NO_LABEL).json()["data"]["policy_id"]
    xyz, analysis = process(client, app, tenant.admin, undated("65%", [Section("8", "Top-up Loans", ["Top-up loans are allowed after 12 months."])]))
    response = confirm_new_version(admin, xyz, analysis, policy_id, version=NO_LABEL, reason="new version")
    assert response.status_code == 200, response.text
    drain(app)
    return policy_id, abc, xyz


@pytest.fixture
def home_loan(client, app, tenant, admin):
    v3, analysis = process(client, app, tenant.admin, build(V3))
    policy_id = confirm_new_policy(admin, v3, analysis).json()["data"]["policy_id"]
    v4, analysis = process(client, app, tenant.admin, build(V4))
    assert confirm_new_version(admin, v4, analysis, policy_id).status_code == 200
    drain(app)
    return policy_id


def versions_by_document(admin, policy_id):
    return {v["document_id"]: v for v in admin.get(f"/policies/{policy_id}").json()["data"]["versions"]}


# --- §18 drag-and-drop version order -------------------------------------------------------


def test_dragging_a_version_to_the_top_makes_it_latest_after_confirmation(db, admin, tenant, gold):
    policy_id, abc, xyz = gold
    before = versions_by_document(admin, policy_id)
    assert (before[str(abc)]["version_label"], before[str(abc)]["timeline_state"]) == ("1", "historical")
    assert (before[str(xyz)]["version_label"], before[str(xyz)]["timeline_state"]) == ("2", "current")
    order = [before[str(abc)]["id"], before[str(xyz)]["id"]]  # abc dragged to the top

    unconfirmed = admin.put(f"/policies/{policy_id}/versions/order", json={"version_ids": order})
    assert unconfirmed.status_code == 409
    error = unconfirmed.json()["error"]
    assert error["code"] == "LATEST_CHANGE_REQUIRES_CONFIRMATION"
    assert (error["details"]["current"]["label"], error["details"]["new"]["label"]) == ("2", "1")

    confirmed = admin.put(f"/policies/{policy_id}/versions/order", json={"version_ids": order, "confirm_latest_change": True})
    assert confirmed.status_code == 200, confirmed.text
    after = versions_by_document(admin, policy_id)
    # The spec's example: abc.pdf is now v2 and active, xyz.pdf v1 and archived.
    assert (after[str(abc)]["version_label"], after[str(abc)]["timeline_state"]) == ("2", "current")
    assert (after[str(xyz)]["version_label"], after[str(xyz)]["timeline_state"]) == ("1", "historical")
    assert after[str(xyz)]["superseded_by_version_id"] == after[str(abc)]["id"]
    event = db.scalar(select(AuditEvent).where(AuditEvent.action == "policy.latest_version_changed"))
    assert event.details["old_version_id"] == before[str(xyz)]["id"]
    assert event.details["new_version_id"] == before[str(abc)]["id"]

    # Unchanged order is a no-op; the list must name every active version once.
    same = [after[str(abc)]["id"], after[str(xyz)]["id"]]
    assert admin.put(f"/policies/{policy_id}/versions/order", json={"version_ids": same}).status_code == 200
    assert admin.put(f"/policies/{policy_id}/versions/order", json={"version_ids": same[:1]}).status_code == 422


def test_order_never_overrides_a_stated_effective_date(admin, home_loan):
    versions = admin.get(f"/policies/{home_loan}").json()["data"]["versions"]
    v3, v4 = versions
    response = admin.put(f"/policies/{home_loan}/versions/order",
                         json={"version_ids": [v3["id"], v4["id"]], "confirm_latest_change": True})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VERSION_ORDER_CONFLICT"
    assert "Change its effective date" in response.json()["error"]["message"]


def test_users_cannot_reorder(client, tenant, gold):
    policy_id, _, _ = gold
    assert login(client, tenant.user_a1).put(f"/policies/{policy_id}/versions/order", json={"version_ids": []}).status_code == 403


# --- §11 create a policy by hand, §19 move a document between policies ----------------------


def test_policies_are_created_by_hand_with_an_explicit_scope(client, db, admin, tenant):
    manager = login(client, tenant.manager_a)
    body = {"name": "Employee Leave Policy", "category_id": category_id(db, tenant)}
    assert manager.post("/policies", json={**body, "scope": "global"}).status_code == 403
    branch = manager.post("/policies", json={**body, "scope": "branch"})
    assert branch.status_code == 201, branch.text
    assert branch.json()["data"]["branch_id"] == str(tenant.branch_a.id)
    assert manager.post("/policies", json={**body, "scope": "branch", "branch_id": str(tenant.branch_b.id)}).status_code == 403
    global_policy = admin.post("/policies", json={**body, "name": "HR Leave Policy", "scope": "global"})
    assert global_policy.status_code == 201 and global_policy.json()["data"]["branch_id"] is None
    assert admin.get(f"/policies/{global_policy.json()['data']['id']}").json()["data"]["version_count"] == 0


def test_moving_a_version_to_another_policy(db, app, admin, tenant, home_loan):
    target = admin.post("/policies", json={
        "name": "NRI Home Loan Policy", "category_id": category_id(db, tenant), "scope": "global",
    }).json()["data"]["id"]
    v3, v4 = admin.get(f"/policies/{home_loan}").json()["data"]["versions"]

    unconfirmed = admin.post(f"/policies/{home_loan}/versions/{v4['id']}/move", json={"target_policy_id": target})
    assert unconfirmed.json()["error"]["code"] == "MOVE_REQUIRES_CONFIRMATION"
    moved = admin.post(f"/policies/{home_loan}/versions/{v4['id']}/move", json={"target_policy_id": target, "confirm": True})
    assert moved.status_code == 200, moved.text
    assert moved.json()["data"]["policy_id"] == target and moved.json()["data"]["status"] == "active"

    source = admin.get(f"/policies/{home_loan}").json()["data"]
    assert [v["version_label"] for v in source["versions"] if v["status"] == "active"] == ["3"]
    assert source["current_version"]["id"] == v3["id"] and source["current_version"]["effective_to"] is None
    document = db.get(Document, v4["document_id"])
    db.refresh(document)
    assert str(document.policy_id) == target
    assert {str(p) for p in db.scalars(select(Chunk.policy_id).where(Chunk.document_id == document.id))} == {target}
    event = db.scalar(select(AuditEvent).where(AuditEvent.action == "document.moved"))
    assert event.details["from_policy"] == "Home Loan Credit Policy" and event.details["to_policy"] == "NRI Home Loan Policy"


# --- assignments, notifications ------------------------------------------------------------


def test_assignment_rules_history_and_notifications(client, db, admin, tenant, home_loan):
    manager = login(client, tenant.manager_a)
    candidates = manager.get(f"/policies/{home_loan}/assignable-users").json()["data"]
    assert {u["id"] for u in candidates} == {str(tenant.user_a1.id), str(tenant.user_a2.id)}  # own branch only

    assert manager.post(f"/policies/{home_loan}/assignments", json={"user_ids": [str(tenant.user_a1.id)]}).status_code == 200
    user = login(client, tenant.user_a1)
    inbox = user.get("/notifications").json()["data"]
    assert inbox["unread"] == 1 and inbox["items"][0]["type"] == "POLICY_ASSIGNED"
    assert user.get(f"/policies/{home_loan}").status_code == 200
    assert [p["id"] for p in user.get("/policies").json()["data"]["items"]] == [home_loan]

    assert manager.delete(f"/policies/{home_loan}/assignments/{tenant.user_a1.id}").status_code == 200
    assert user.get(f"/policies/{home_loan}").status_code == 404
    history = admin.get(f"/users/{tenant.user_a1.id}/assignments?include_removed=true").json()["data"]
    assert len(history) == 1 and history[0]["removed_at"] is not None
    assert history[0]["assigned_by_name"] == tenant.manager_a.full_name
    types = [n["type"] for n in user.get("/notifications").json()["data"]["items"]]
    assert types == ["POLICY_REMOVED", "POLICY_ASSIGNED"]
    assert user.post("/notifications/read", json={}).json()["data"]["updated"] == 2
    assert user.get("/notifications").json()["data"]["unread"] == 0


def test_branch_policy_cannot_be_assigned_across_branches(client, db, admin, tenant):
    manager_b = login(client, tenant.manager_b)
    policy = manager_b.post("/policies", json={
        "name": "Branch B Cash Handling", "category_id": category_id(db, tenant), "scope": "branch",
    }).json()["data"]["id"]
    response = admin.post(f"/policies/{policy}/assignments", json={"user_ids": [str(tenant.user_a1.id)]})
    assert response.status_code == 422 and response.json()["error"]["code"] == "OUT_OF_SCOPE"
    assert login(client, tenant.manager_a).get(f"/policies/{policy}/assignable-users").status_code == 404


def test_publishing_notifies_managers_and_assigned_users(client, app, db, admin, tenant):
    v3, analysis = process(client, app, tenant.admin, build(V3))
    policy_id = confirm_new_policy(admin, v3, analysis).json()["data"]["policy_id"]
    drain(app)
    for manager in (tenant.manager_a, tenant.manager_b):
        items = login(client, manager).get("/notifications").json()["data"]["items"]
        assert items[0]["type"] == "NEW_POLICY" and "Home Loan Credit Policy" in items[0]["title"]
    assert admin.post(f"/policies/{policy_id}/assignments", json={"user_ids": [str(tenant.user_a1.id)]}).status_code == 200

    v4, analysis = process(client, app, tenant.admin, build(V4))
    confirm_new_version(admin, v4, analysis, policy_id)
    drain(app)
    items = login(client, tenant.user_a1).get("/notifications").json()["data"]["items"]
    # v4 takes effect on 2026-07-01, which is already past: it is the version in force.
    assert items[0]["type"] == "POLICY_VERSION_CHANGED"
    assert "75% → 70%" in items[0]["body"]
    drain(app)  # a retried publish never notifies twice
    assert db.scalar(select(Notification.id).where(Notification.user_id == tenant.user_b1.id)) is None


def test_expiry_sweep_warns_once(client, app, db, admin, tenant, home_loan):
    from app.modules.notifications.service import expiry_sweep

    admin.post(f"/policies/{home_loan}/assignments", json={"user_ids": [str(tenant.user_a1.id)]})
    current = db.scalar(select(PolicyVersion).where(PolicyVersion.policy_id == home_loan, PolicyVersion.effective_to.is_(None)))
    current.effective_to = date.today() + timedelta(days=10)
    db.commit()
    assert expiry_sweep(db, date.today()) >= 1
    db.commit()
    assert expiry_sweep(db, date.today()) == 0  # deduplicated
    items = login(client, tenant.user_a1).get("/notifications").json()["data"]["items"]
    assert items[0]["type"] == "POLICY_EXPIRING"


# --- §37 AI summary --------------------------------------------------------------------------


def test_each_version_gets_a_grounded_ai_summary(admin, home_loan):
    v3, v4 = admin.get(f"/policies/{home_loan}").json()["data"]["versions"]
    assert v4["has_ai_summary"] is True
    summary = admin.get(f"/policies/{home_loan}/versions/{v4['id']}/summary").json()["data"]
    assert summary["status"] == "ready" and summary["ai_generated"] is True
    assert summary["effective_from"] == "2026-07-01" and summary["version_label"] == "4"
    rules = " ".join(r["text"] for r in summary["key_rules"])
    assert "70%" in rules and "75%" not in rules.replace("75 lakh", "")
    assert "Changed: 5.2 LTV for High Value Loans (75% → 70%)" in summary["important_changes"]
    assert summary["change_summary"].startswith("Important changes:")
