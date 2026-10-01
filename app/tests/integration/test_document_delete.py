"""Deleting a document removes it and what was derived from it, and repairs its policy's timeline."""

import uuid

import pytest
from sqlalchemy import func, select

from app.modules.audit.model import AuditEvent
from app.modules.documents.model import Document
from app.modules.policies.model import Policy, PolicyVersion
from app.modules.search.model import Chunk
from app.tests.factories import login, make_tenant
from app.tests.flows import build, confirm_new_policy, confirm_new_version, home_loan_spec, process
from app.tests.pipeline import drain

pytestmark = pytest.mark.integration


@pytest.fixture
def tenant(db):
    return make_tenant(db)


@pytest.fixture
def admin(client, tenant):
    return login(client, tenant.admin)


@pytest.fixture
def home_loan(client, app, tenant, admin):
    """v1 (2024) superseded by v2 (2025, in force)."""
    v1_doc, analysis = process(client, app, tenant.admin, build(home_loan_spec("1", "01/01/2024")))
    policy_id = uuid.UUID(confirm_new_policy(admin, v1_doc, analysis).json()["data"]["policy_id"])
    v2_doc, analysis = process(client, app, tenant.admin, build(home_loan_spec("2", "01/06/2025", ltv="70%")))
    assert confirm_new_version(admin, v2_doc, analysis, policy_id).status_code == 200
    drain(app)
    return policy_id, v1_doc, v2_doc


def test_deleting_the_version_in_force_puts_the_previous_one_back(app, db, admin, home_loan):
    policy_id, v1_doc, v2_doc = home_loan
    key = db.get(Document, v2_doc).storage_key

    response = admin.delete(f"/documents/{v2_doc}")
    assert response.status_code == 200, response.text
    db.expire_all()

    assert db.get(Document, v2_doc) is None
    assert not db.scalar(select(func.count()).select_from(Chunk).where(Chunk.document_id == v2_doc))
    assert not app.state.storage.exists(key)
    [remaining] = db.scalars(select(PolicyVersion).where(PolicyVersion.policy_id == policy_id)).all()
    assert remaining.document_id == v1_doc and remaining.effective_to is None
    assert remaining.superseded_by_version_id is None
    event = db.scalar(select(AuditEvent).where(AuditEvent.action == "document.deleted"))
    assert event.details["version"] == "2" and "policy_deleted" not in event.details

    answer = admin.post("/ai/ask", json={"question": "What is the LTV for home loans above 75 lakh?"}).json()["data"]
    assert answer["status"] == "answered" and {s["version_label"] for s in answer["sources"]} == {"1"}


def test_deleting_the_last_version_deletes_the_policy(db, admin, home_loan):
    policy_id, v1_doc, v2_doc = home_loan
    assert admin.delete(f"/documents/{v2_doc}").status_code == 200
    assert admin.delete(f"/documents/{v1_doc}").status_code == 200
    db.expire_all()
    assert db.get(Policy, policy_id) is None
    assert admin.get(f"/documents/{v1_doc}").status_code == 404


def test_a_department_user_cannot_delete(client, tenant, home_loan):
    _policy_id, _v1, v2_doc = home_loan
    assert login(client, tenant.user_a1).delete(f"/documents/{v2_doc}").status_code in (403, 404)


def test_deleting_a_policy_deletes_every_version_document_and_file(app, db, admin, home_loan):
    policy_id, v1_doc, v2_doc = home_loan
    keys = [db.get(Document, d).storage_key for d in (v1_doc, v2_doc)]

    response = admin.delete(f"/policies/{policy_id}")
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"deleted": True, "versions": 2, "documents": 2}
    db.expire_all()

    assert db.get(Policy, policy_id) is None
    assert db.get(Document, v1_doc) is None and db.get(Document, v2_doc) is None
    assert not db.scalar(select(func.count()).select_from(Chunk).where(Chunk.policy_id == policy_id))
    assert not any(app.state.storage.exists(key) for key in keys)
    event = db.scalar(select(AuditEvent).where(AuditEvent.action == "policy.deleted"))
    assert sorted(event.details["versions"]) == ["1", "2"]
    answer = admin.post("/ai/ask", json={"question": "What is the LTV for home loans above 75 lakh?"}).json()["data"]
    assert answer["status"] == "no_answer"


def test_a_department_user_cannot_delete_a_policy(client, tenant, home_loan):
    policy_id, _v1, _v2 = home_loan
    assert login(client, tenant.user_a1).delete(f"/policies/{policy_id}").status_code in (403, 404)
