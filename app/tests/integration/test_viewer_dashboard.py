import pytest
from sqlalchemy import select

from app.modules.search.model import Chunk
from app.tests.factories import login, make_tenant
from app.tests.flows import V3, build, confirm_new_policy, process
from app.tests.pipeline import drain

pytestmark = pytest.mark.integration


@pytest.fixture
def ready(client, app, db):
    tenant = make_tenant(db)
    admin = login(client, tenant.admin)
    document_id, analysis = process(client, app, tenant.admin, build(V3))
    confirm_new_policy(admin, document_id, analysis)
    drain(app)
    return tenant, admin, document_id


def test_page_image_with_citation_highlight(db, ready):
    _, admin, document_id = ready
    chunk = db.scalar(select(Chunk).where(Chunk.document_id == document_id, Chunk.section_number == "5.2"))
    plain = admin.get(f"/documents/{document_id}/pages/1/image")
    assert plain.status_code == 200 and plain.headers["content-type"] == "image/png"
    assert plain.content[:8] == b"\x89PNG\r\n\x1a\n" and plain.headers["x-highlights"] == "0"
    highlighted = admin.get(f"/documents/{document_id}/pages/1/image?highlight_chunk={chunk.id}")
    assert int(highlighted.headers["x-highlights"]) >= 1
    assert admin.get(f"/documents/{document_id}/pages/9/image").status_code == 404
    assert admin.get(f"/documents/{document_id}/pages/1/image?zoom=10").status_code == 422


def test_outline_page_text_and_chunk(db, ready):
    _, admin, document_id = ready
    outline = admin.get(f"/documents/{document_id}/outline").json()["data"]
    assert [e["number"] for e in outline] == ["1", "2", "5", "5.2", "6"]
    assert "HOME LOAN CREDIT POLICY" in admin.get(f"/documents/{document_id}/pages/1").json()["data"]["text"]
    chunk = db.scalar(select(Chunk).where(Chunk.document_id == document_id))
    assert admin.get(f"/documents/{document_id}/chunks/{chunk.id}").json()["data"]["id"] == str(chunk.id)


def test_viewer_respects_acl(client, db, ready):
    tenant, admin, document_id = ready
    from app.modules.documents.model import Document
    from app.tests.factories import assign

    # Org-wide document: managers can view; a User only once its policy is assigned to them.
    assert login(client, tenant.manager_b).get(f"/documents/{document_id}/pages/1/image").status_code == 200
    user = login(client, tenant.user_b1)
    assert user.get(f"/documents/{document_id}/pages/1/image").status_code == 404
    assign(db, tenant.user_b1, db.get(Document, document_id).policy_id)
    assert user.get(f"/documents/{document_id}/pages/1/image").status_code == 200


def test_dashboard_counts(client, app, ready):
    tenant, admin, _ = ready
    data = admin.get("/dashboard").json()["data"]
    assert data["counts"]["active_policies"] == 1 and data["counts"]["ready_documents"] == 1
    assert data["recent_changes"][0]["policy_name"] == "Home Loan Credit Policy"
    process(client, app, tenant.manager_a, build(V3), allow_duplicate=True, duplicate_reason="dup for test")
    data = admin.get("/dashboard").json()["data"]
    assert data["counts"]["requires_review"] == 1
    assert data["attention"][0]["status"] == "awaiting_confirmation"
