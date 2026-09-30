import pytest

from app.tests.factories import assign, login, make_tenant
from app.tests.flows import V3, build, confirm_new_policy, process
from app.tests.pdfs import PolicySpec, Section
from app.tests.pipeline import drain

pytestmark = pytest.mark.integration

SECRET = PolicySpec(
    title="TREASURY LIMITS POLICY",
    header_lines=["Policy No: TR-2026-07", "Effective Date: 01/01/2026"],
    sections=[Section("1", "Dealer Limits", ["The overnight dealer limit is Rs. 50 crore per desk."])],
)


@pytest.fixture
def world(client, app, db):
    bank_a, bank_b = make_tenant(db, "bank-a"), make_tenant(db, "bank-b")
    # Bank A: org-wide home loan policy + a treasury policy restricted to department A2.
    doc, analysis = process(client, app, bank_a.admin, build(V3))
    home_loan = confirm_new_policy(login(client, bank_a.admin), doc, analysis).json()["data"]["policy_id"]
    doc, analysis = process(client, app, bank_a.admin, build(SECRET),
                            branch_id=bank_a.branch_a.id, department_id=bank_a.dept_a2.id)
    treasury = confirm_new_policy(login(client, bank_a.manager_a), doc, analysis).json()["data"]["policy_id"]
    # Bank B: the same treasury policy text in another tenant.
    doc, analysis = process(client, app, bank_b.admin, build(SECRET))
    confirm_new_policy(login(client, bank_b.admin), doc, analysis)
    drain(app)
    assign(db, bank_a.user_a1, home_loan)
    assign(db, bank_a.user_b1, home_loan)
    assign(db, bank_a.user_a2, treasury)
    return bank_a, bank_b, {"home_loan": home_loan, "treasury": treasury}


def passages(client, user, query):
    response = login(client, user).post("/search", json={"query": query})
    assert response.status_code == 200, response.text
    return response.json()["data"]["passages"]


def test_department_restricted_content_never_reaches_other_departments(client, world):
    bank_a, _, _ = world
    assert any("dealer limit" in p["text"] for p in passages(client, bank_a.user_a2, "overnight dealer limit"))
    for outsider in (bank_a.user_a1, bank_a.user_b1, bank_a.manager_b):
        leaked = [p for p in passages(client, outsider, "overnight dealer limit per desk") if "dealer" in p["text"]]
        assert leaked == []


def test_org_wide_content_reaches_managers_and_assigned_users_only_in_its_tenant(client, world):
    bank_a, bank_b, _ = world
    for user in (bank_a.user_a1, bank_a.user_b1, bank_a.manager_b):
        assert any("LTV" in p["text"] for p in passages(client, user, "LTV high value loans"))
    # user_a2 is not assigned the home loan policy: organisation-wide is not enough for a User.
    assert all("LTV" not in p["text"] for p in passages(client, bank_a.user_a2, "LTV high value loans"))
    assert all("LTV" not in p["text"] for p in passages(client, bank_b.admin, "LTV high value loans"))


def test_assignment_cannot_reach_past_the_department_scope(client, db, world):
    bank_a, _, policies = world
    assign(db, bank_a.user_a1, policies["treasury"])  # department A2 policy, user in A1
    leaked = [p for p in passages(client, bank_a.user_a1, "overnight dealer limit per desk") if "dealer" in p["text"]]
    assert leaked == []


def test_other_tenant_never_sees_bank_a_results(client, world):
    bank_a, bank_b, _ = world
    tenant_docs = {p["source"]["document_id"] for p in passages(client, bank_b.admin, "overnight dealer limit")}
    bank_a_docs = {p["source"]["document_id"] for p in passages(client, bank_a.admin, "overnight dealer limit")}
    assert tenant_docs and bank_a_docs and not (tenant_docs & bank_a_docs)


def test_master_admin_cannot_search(client, db, world):
    from app.modules.auth.permissions import Role
    from app.tests.factories import make_user

    master = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    assert login(client, master).post("/search", json={"query": "limit"}).status_code == 403
