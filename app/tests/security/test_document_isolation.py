import io

import pytest

from app.tests.factories import assign, attach_to_policy, login, make_tenant
from app.tests.pdfs import PolicySpec, build_policy_pdf

pytestmark = pytest.mark.integration


def upload(session, title, **form):
    pdf = build_policy_pdf(PolicySpec(title=title))
    response = session.post(
        "/documents",
        files={"file": ("f.pdf", io.BytesIO(pdf), "application/pdf")},
        data={k: str(v) for k, v in form.items()},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


@pytest.fixture
def world(client, db):
    bank_a, bank_b = make_tenant(db, "bank-a"), make_tenant(db, "bank-b")
    admin = login(client, bank_a.admin)
    docs = {
        "a_org_wide": upload(admin, "ORG WIDE POLICY"),
        "a_branch_a": upload(login(client, bank_a.manager_a), "BRANCH A POLICY"),
        "a_dept_a1": upload(admin, "DEPT A1 POLICY", branch_id=bank_a.branch_a.id, department_id=bank_a.dept_a1.id),
        "a_dept_a2": upload(admin, "DEPT A2 POLICY", branch_id=bank_a.branch_a.id, department_id=bank_a.dept_a2.id),
        "a_branch_b": upload(login(client, bank_a.manager_b), "BRANCH B POLICY"),
        "a_unfiled": upload(admin, "NOT YET A POLICY"),
        "b_org_wide": upload(login(client, bank_b.admin), "OTHER BANK POLICY"),
    }
    policies = {key: attach_to_policy(db, doc_id).id for key, doc_id in docs.items() if key != "a_unfiled"}
    # user_a1 is assigned everything in bank A, including policies whose scope does not
    # reach them (another department, another branch): scope must still apply.
    assign(db, bank_a.user_a1, *(policies[k] for k in ("a_org_wide", "a_branch_a", "a_dept_a1", "a_dept_a2", "a_branch_b")))
    assign(db, bank_a.user_b1, policies["a_branch_b"])
    return bank_a, bank_b, docs


EXPECTED = {
    "admin": {"a_org_wide", "a_branch_a", "a_dept_a1", "a_dept_a2", "a_branch_b", "a_unfiled"},
    "manager_a": {"a_org_wide", "a_branch_a", "a_dept_a1", "a_dept_a2", "a_unfiled"},
    # Users: assigned AND within their branch/department scope; never unfiled documents.
    "user_a1": {"a_org_wide", "a_branch_a", "a_dept_a1"},
    "user_a2": set(),  # nothing assigned: sees nothing, not even organisation-wide policies
    "user_b1": {"a_branch_b"},
}


@pytest.mark.parametrize("who", list(EXPECTED))
def test_document_visibility_matrix(client, world, who):
    bank_a, _, docs = world
    session = login(client, getattr(bank_a, who))
    listed = {d["id"] for d in session.get("/documents?limit=200").json()["data"]["items"]}
    expected_ids = {docs[k] for k in EXPECTED[who]}
    assert listed == expected_ids

    for key, doc_id in docs.items():
        visible = key in EXPECTED[who]
        assert (session.get(f"/documents/{doc_id}").status_code == 200) == visible
        assert (session.get(f"/documents/{doc_id}/file").status_code == 200) == visible
        assert (session.get(f"/documents/{doc_id}/progress").status_code == 200) == visible


def test_users_cannot_upload(client, db):
    tenant = make_tenant(db)
    response = login(client, tenant.user_a1).post(
        "/documents", files={"file": ("f.pdf", io.BytesIO(build_policy_pdf()), "application/pdf")},
    )
    assert response.status_code == 403
