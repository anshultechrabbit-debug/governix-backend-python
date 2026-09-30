import io
import uuid

import pytest
from sqlalchemy import func, select

from app.infrastructure.ai.ocr.base import OCRProvider, OCRResult
from app.modules.documents.model import Document, DocumentPage, DocumentSection
from app.tests.factories import login, make_tenant
from app.tests.pdfs import (
    PolicySpec,
    build_encrypted_pdf,
    build_policy_pdf,
    build_scanned_pdf,
)
from app.tests.pipeline import drain, jobs
from app.workers.runtime import get_runtime

pytestmark = pytest.mark.integration


class FakeOCR(OCRProvider):
    name = "fake-ocr"

    def __init__(self, available=True):
        self.available = available
        self.pages = []

    def is_available(self):
        return self.available

    def ocr_page(self, page):
        self.pages.append(page.number + 1)
        return OCRResult(
            text=f"1 Scanned Circular\n\nAll branches are informed that page {page.number + 1} was scanned.",
            engine=self.name,
        )


@pytest.fixture
def tenant(db):
    return make_tenant(db)


def upload(client, tenant, data):
    response = login(client, tenant.admin).post(
        "/documents", files={"file": ("doc.pdf", io.BytesIO(data), "application/pdf")}
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["data"]["id"])


def stages(db, document_id):
    from app.modules.ingestion.progress import snapshot

    db.expire_all()
    return {s["stage"]: s for s in snapshot(db, document_id)["stages"]}


def test_policy_pdf_is_extracted_and_structured(client, db, tenant, app):
    document_id = upload(client, tenant, build_policy_pdf())
    drain(app)

    document = db.get(Document, document_id)
    assert document.status == "awaiting_confirmation"
    assert document.page_count == 1
    assert document.content_hash and document.simhash is not None
    assert document.pdf_metadata["title"] == "loan_policy_final_v7"

    sections = db.scalars(
        select(DocumentSection).where(DocumentSection.document_id == document_id)
        .order_by(DocumentSection.order_index)
    ).all()
    assert [s.number for s in sections] == [None, "1", "2", "5", "5.2", "6"]
    front, *_ , ltv, high, rate = sections
    assert "HOME LOAN CREDIT POLICY" in front.content and "HL-2025-01" in front.content
    assert high.parent_id == ltv.id
    assert "75%" in high.content
    assert rate.title == "Interest Rate"

    s = stages(db, document_id)
    assert s["extraction"]["status"] == "completed" and s["extraction"]["done_units"] == 1
    assert s["ocr"]["status"] == "skipped"
    assert s["structure"]["status"] == "completed"
    assert s["structure"]["detail"]["sections"] == 6
    assert len(jobs(app, "ingestion.analyze")) == 1


def test_large_pdf_is_split_into_parallel_ranges(client, db, tenant, app):
    app.state.settings.EXTRACTION_BATCH_PAGES = 2
    document_id = upload(client, tenant, build_policy_pdf(PolicySpec(extra_pages=6)))
    drain(app)

    ranges = sorted((j.payload["start"], j.payload["end"]) for j in jobs(app, "ingestion.extract_range"))
    assert ranges == [(1, 2), (3, 4), (5, 6), (7, 7)]
    assert db.scalar(select(func.count()).select_from(DocumentPage).where(
        DocumentPage.document_id == document_id)) == 7
    # Every range checks for completion, but structure runs exactly once.
    assert len(jobs(app, "ingestion.structure")) == 1


def test_extract_range_is_idempotent_on_rerun(client, db, tenant, app):
    from app.infrastructure.queue.registry import JobContext
    from app.workers.extraction.tasks import extract_range

    document_id = upload(client, tenant, build_policy_pdf(PolicySpec(extra_pages=2)))
    drain(app)
    payload = {"document_id": str(document_id), "attempt": 1, "start": 1, "end": 3}
    ctx = JobContext(uuid.uuid4(), 1, 3, None, heartbeat=lambda: None)
    extract_range(payload, ctx)  # rerun after completion: nothing duplicated
    assert db.scalar(select(func.count()).select_from(DocumentPage).where(
        DocumentPage.document_id == document_id)) == 3


def test_scanned_pages_are_routed_to_ocr(client, db, tenant, app):
    fake = FakeOCR()
    get_runtime().overrides["ocr"] = fake
    document_id = upload(client, tenant, build_scanned_pdf(pages=2))
    drain(app)

    assert fake.pages == [1, 2]
    pages = db.scalars(select(DocumentPage).where(DocumentPage.document_id == document_id)).all()
    assert {p.method for p in pages} == {"ocr"}
    s = stages(db, document_id)
    assert s["ocr"]["status"] == "completed" and s["ocr"]["done_units"] == 2
    assert s["ocr"]["detail"]["ocr_pages"] == 2
    assert s["structure"]["status"] == "completed"


def test_scanned_pdf_without_ocr_engine_fails_with_clear_reason(client, db, tenant, app):
    get_runtime().overrides["ocr"] = FakeOCR(available=False)
    document_id = upload(client, tenant, build_scanned_pdf(pages=3))
    drain(app)

    document = db.get(Document, document_id)
    assert document.status == "failed"
    assert document.error["code"] == "OCR_UNAVAILABLE"
    assert "3 scanned pages" in document.error["message"]
    assert stages(db, document_id)["structure"]["status"] == "failed"


@pytest.mark.parametrize("data, code", [
    (build_encrypted_pdf(), "ENCRYPTED_PDF"),
    (b"%PDF-1.7\n" + b"garbage" * 100, "CORRUPT_PDF"),
])
def test_unreadable_pdfs_fail_permanently(client, db, tenant, app, data, code):
    document_id = upload(client, tenant, data)
    drain(app)
    document = db.get(Document, document_id)
    assert document.status == "failed"
    assert document.error["code"] == code
    assert len(jobs(app, "ingestion.inspect")) == 1  # not retried


def test_retry_after_failure_uses_fresh_jobs(client, db, tenant, app):
    get_runtime().overrides["ocr"] = FakeOCR(available=False)
    document_id = upload(client, tenant, build_scanned_pdf())
    drain(app)
    assert db.get(Document, document_id).status == "failed"

    get_runtime().overrides["ocr"] = FakeOCR()
    del get_runtime().__dict__["ocr"]  # drop cached engine
    response = login(client, tenant.admin).post(f"/documents/{document_id}/retry")
    assert response.status_code == 200, response.text
    drain(app)
    db.expire_all()
    document = db.get(Document, document_id)
    assert document.ingestion_attempt == 2
    assert document.status == "awaiting_confirmation" and document.error is None
    pages = db.scalars(select(DocumentPage).where(DocumentPage.document_id == document_id)).all()
    assert {p.method for p in pages} == {"ocr"}
    assert stages(db, document_id)["structure"]["status"] == "completed"
