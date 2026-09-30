import hashlib
import io

import pytest
from sqlalchemy import select

from app.infrastructure.queue.models import QueueJob
from app.modules.audit.model import AuditEvent
from app.modules.documents.model import Document
from app.tests.factories import login, make_tenant
from app.tests.pdfs import build_policy_pdf

pytestmark = pytest.mark.integration

PDF = build_policy_pdf()


def upload(session, data=PDF, filename="loan_policy_final_v7.pdf", **form):
    return session.post(
        "/documents",
        files={"file": (filename, io.BytesIO(data), "application/pdf")},
        data={k: str(v) for k, v in form.items()},
    )


@pytest.fixture
def tenant(db):
    return make_tenant(db)


def test_upload_registers_document_stores_file_and_enqueues_pipeline(client, db, tenant, app):
    response = upload(login(client, tenant.manager_a))
    assert response.status_code == 201, response.text
    doc = response.json()["data"]

    assert doc["status"] == "uploaded"
    assert doc["title"] is None  # never derived from the filename
    assert doc["original_filename"] == "loan_policy_final_v7.pdf"
    assert doc["file_sha256"] == hashlib.sha256(PDF).hexdigest()
    # Branch managers upload into their own branch by default.
    assert doc["branch_id"] == str(tenant.branch_a.id)
    assert doc["department_id"] is None
    stages = {s["stage"]: s["status"] for s in doc["progress"]["stages"]}
    assert stages["upload"] == stages["validation"] == "completed"
    assert stages["extraction"] == "pending"

    stored = db.get(Document, doc["id"])
    with app.state.storage.open(stored.storage_key) as handle:
        assert handle.read() == PDF
    job = db.scalar(select(QueueJob))
    assert job.task_name == "ingestion.inspect" and job.payload["document_id"] == doc["id"]


@pytest.mark.parametrize("data, filename, code", [
    (b"not a pdf at all", "x.pdf", "UNSUPPORTED_FILE_TYPE"),
    (PDF, "policy.docx", "UNSUPPORTED_FILE_TYPE"),
])
def test_rejects_non_pdf(client, tenant, data, filename, code, db):
    response = upload(login(client, tenant.manager_a), data=data, filename=filename)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code
    assert db.scalar(select(Document)) is None


def test_enforces_size_limit_and_cleans_up(client, db, tenant, app, settings):
    app.state.settings.MAX_UPLOAD_SIZE_MB = 0  # anything non-empty is too large
    response = upload(login(client, tenant.manager_a))
    assert response.status_code == 413
    originals = app.state.storage.root / "originals"
    assert not any(p.is_file() for p in originals.rglob("*"))


def test_exact_duplicate_is_blocked_then_allowed_with_reason(client, db, tenant):
    session = login(client, tenant.manager_a)
    first = upload(session).json()["data"]

    duplicate = upload(session, filename="home_policy_final.pdf")
    assert duplicate.status_code == 409
    error = duplicate.json()["error"]
    assert error["code"] == "DUPLICATE_DOCUMENT"
    assert error["details"]["existing"]["document_id"] == first["id"]

    no_reason = upload(session, allow_duplicate=True)
    assert no_reason.json()["error"]["code"] == "DUPLICATE_REASON_REQUIRED"

    forced = upload(session, allow_duplicate=True, duplicate_reason="Re-issued copy for audit file")
    assert forced.status_code == 201
    assert forced.json()["data"]["duplicate_of_id"] == first["id"]
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "document.duplicate_override"))


def test_duplicate_in_invisible_scope_reveals_nothing(client, tenant):
    upload(login(client, tenant.manager_b))  # branch B
    response = upload(login(client, tenant.manager_a))  # branch A cannot see it
    error = response.json()["error"]
    assert error["code"] == "DUPLICATE_DOCUMENT"
    assert "details" not in error


def test_uploader_cannot_target_scope_outside_their_own(client, tenant):
    # Users do not upload at all (spec access matrix).
    assert upload(login(client, tenant.user_a1)).status_code == 403
    assert upload(
        login(client, tenant.manager_a), branch_id=tenant.branch_b.id
    ).status_code == 403
    admin_upload = upload(login(client, tenant.admin))
    assert admin_upload.json()["data"]["branch_id"] is None  # org-wide by default


def test_download_is_audited(client, db, tenant):
    session = login(client, tenant.manager_a)
    doc_id = upload(session).json()["data"]["id"]
    response = session.get(f"/documents/{doc_id}/file")
    assert response.status_code == 200
    assert response.content == PDF
    assert response.headers["content-type"] == "application/pdf"
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "document.accessed"))


def test_archive_and_retry_rules(client, tenant):
    owner = login(client, tenant.manager_a)
    doc_id = upload(owner).json()["data"]["id"]
    assert owner.post(f"/documents/{doc_id}/retry").json()["error"]["code"] == "INVALID_STATE"
    # Users cannot manage uploads at all; another branch's manager cannot even see it.
    assert login(client, tenant.user_a2).post(f"/documents/{doc_id}/archive").status_code == 403
    assert login(client, tenant.manager_b).post(f"/documents/{doc_id}/archive").status_code == 404
    assert owner.post(f"/documents/{doc_id}/archive").json()["data"]["status"] == "archived"


def test_categories_seeded_and_admin_managed(client, tenant):
    user = login(client, tenant.user_a1)
    names = [c["name"] for c in user.get("/categories").json()["data"]]
    assert names[0] == "Regulatory Documents" and "Policies" in names and len(names) == 10
    assert user.post("/categories", json={"name": "Memos", "slug": "memos"}).status_code == 403

    admin = login(client, tenant.admin)
    created = admin.post("/categories", json={
        "name": "Board Memos", "slug": "board-memos", "category_type": "notice",
        "keywords": [" Board Memo ", "board memo"],
    })
    assert created.status_code == 201
    assert created.json()["data"]["keywords"] == ["board memo"]
    assert admin.post("/categories", json={"name": "Dup", "slug": "board-memos", "category_type": "other"}).status_code == 409


def test_duplicates_are_detected_before_upload(client, tenant):
    session = login(client, tenant.manager_a)
    first = upload(session).json()["data"]
    unknown = "0" * 64
    results = {r["sha256"]: r for r in session.post("/documents/duplicates", json={
        "sha256": [first["file_sha256"], unknown],
    }).json()["data"]}
    assert results[first["file_sha256"]]["duplicate"] is True
    assert results[first["file_sha256"]]["existing"]["document_id"] == first["id"]
    assert results[unknown]["duplicate"] is False
    # Branch B's manager learns only that it exists somewhere they cannot see.
    other = login(client, tenant.manager_b).post("/documents/duplicates", json={"sha256": [first["file_sha256"]]}).json()["data"][0]
    assert other["duplicate"] is True and other["restricted"] is True and other["existing"] is None
    limits = session.get("/documents/upload-limits").json()["data"]
    assert limits["accepted_extensions"] == [".pdf"] and limits["max_size_bytes"] > 0
    assert login(client, tenant.user_a1).get("/documents/upload-limits").status_code == 403
