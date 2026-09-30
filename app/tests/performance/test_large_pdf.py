"""Opt-in: pytest -m performance app/tests/performance -s"""

import io
import resource
import time

import pymupdf
import pytest
from sqlalchemy import func, select

from app.modules.documents.model import Document, DocumentPage, DocumentSection
from app.tests.factories import login, make_tenant
from app.tests.pipeline import drain

pytestmark = [pytest.mark.integration, pytest.mark.performance]

PAGES = 2000


def build_large_pdf(pages: int) -> bytes:
    doc = pymupdf.open()
    for n in range(1, pages + 1):
        page = doc.new_page()
        if n % 10 == 1:
            page.insert_text((72, 72), f"{n // 10 + 1} Chapter Heading {n // 10 + 1}", fontsize=13, fontname="hebo")
        y = 100
        for line in range(40):
            page.insert_text((72, y), f"Clause text line {line} on page {n}: the limit is Rs. {n * 1000:,}.", fontsize=10)
            y += 16
        page.insert_text((72, 800), f"Page {n} of {pages}", fontsize=8)
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data


def test_large_pdf_extraction_throughput(client, db, app):
    tenant = make_tenant(db)
    app.state.settings.EXTRACTION_BATCH_PAGES = 250
    data = build_large_pdf(PAGES)
    response = login(client, tenant.admin).post(
        "/documents", files={"file": ("big.pdf", io.BytesIO(data), "application/pdf")}
    )
    document_id = response.json()["data"]["id"]

    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.perf_counter()
    drain(app, max_jobs=100)
    elapsed = time.perf_counter() - started
    rss_growth_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss_before) / (1024 * 1024)

    pages = db.scalar(select(func.count()).select_from(DocumentPage).where(DocumentPage.document_id == document_id))
    sections = db.scalar(select(func.count()).select_from(DocumentSection).where(DocumentSection.document_id == document_id))
    print(f"\n{PAGES} pages ({len(data) / 1e6:.1f} MB): {elapsed:.1f}s total, "
          f"{PAGES / elapsed:.0f} pages/s single worker, {sections} sections, "
          f"peak RSS growth {rss_growth_mb:.0f} MB")
    assert pages == PAGES
    assert db.get(Document, document_id).status == "awaiting_confirmation"
    assert sections >= PAGES // 10
