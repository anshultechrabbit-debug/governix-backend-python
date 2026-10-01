"""Opt-in: pytest -m performance app/tests/performance -s"""

try:
    import resource
except ImportError:
    resource = None
import io
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

    rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss if resource else 0
    started = time.perf_counter()
    drain(app, max_jobs=100)
    elapsed = time.perf_counter() - started
    rss_growth_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - rss_before) / (1024 * 1024) if resource else 0.0

    pages = db.scalar(select(func.count()).select_from(DocumentPage).where(DocumentPage.document_id == document_id))
    sections = db.scalar(select(func.count()).select_from(DocumentSection).where(DocumentSection.document_id == document_id))
    print(f"\n{PAGES} pages ({len(data) / 1e6:.1f} MB): {elapsed:.1f}s total, "
          f"{PAGES / elapsed:.0f} pages/s single worker, {sections} sections, "
          f"peak RSS growth {rss_growth_mb:.0f} MB")
    assert pages == PAGES
    assert db.get(Document, document_id).status == "awaiting_confirmation"
    assert sections >= PAGES // 10


def build_document(pages: int) -> bytes:
    """A policy-like PDF: a heading every 10 pages, prose on every page, a ruled table every 50."""
    doc = pymupdf.open()
    for n in range(1, pages + 1):
        page = doc.new_page()
        if n % 10 == 1:
            page.insert_text((72, 72), f"{n // 10 + 1} Section Heading {n // 10 + 1}", fontsize=13, fontname="hebo")
        y = 100
        for line in range(20):
            page.insert_text((72, y), f"Rule {line} on page {n}: the customer limit is Rs. {n * 1000:,} for item {line}.", fontsize=10)
            y += 16
        if n % 50 == 0:
            for row in range(4):
                for col in range(3):
                    rect = pymupdf.Rect(72 + col * 150, 450 + row * 20, 222 + col * 150, 470 + row * 20)
                    page.draw_rect(rect, width=0.5)
                    page.insert_text((rect.x0 + 4, rect.y1 - 6), f"Cell {row}-{col} p{n}", fontsize=8)
        page.insert_text((72, 800), f"Page {n} of {pages}", fontsize=8)
    data = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return data


def test_full_pipeline_timing(client, db, app):
    """Upload -> extraction -> structure -> analysis -> confirm -> chunk -> embed -> index -> verify.

    Size with GOVERNIX_PERF_PAGES (default 2000), e.g. GOVERNIX_PERF_PAGES=10500. Jobs run one
    at a time here (a single worker), so this is the slowest case; a server runs several.
    """
    import os

    from app.modules.ingestion.model import IngestionStage
    from app.modules.search.model import Chunk
    from app.tests.flows import confirm_new_policy

    pages = int(os.environ.get("GOVERNIX_PERF_PAGES", "2000"))
    tenant = make_tenant(db)
    admin = login(client, tenant.admin)
    built = time.perf_counter()
    data = build_document(pages)
    built = time.perf_counter() - built

    started = time.perf_counter()
    response = admin.post("/documents", files={"file": ("big.pdf", io.BytesIO(data), "application/pdf")})
    assert response.status_code == 201, response.text
    document_id = response.json()["data"]["id"]
    drain(app, max_jobs=10_000)
    to_review = time.perf_counter() - started
    analysis = admin.get(f"/documents/{document_id}/analysis").json()["data"]
    assert confirm_new_policy(admin, document_id, analysis).status_code == 200
    indexing = time.perf_counter()
    drain(app, max_jobs=10_000)
    finished = time.perf_counter()

    db.expire_all()
    assert db.get(Document, document_id).status == "ready"
    stages = db.scalars(select(IngestionStage).where(IngestionStage.document_id == document_id)).all()
    chunks = db.scalar(select(func.count()).select_from(Chunk).where(Chunk.document_id == document_id))
    print(f"\n{pages} pages, {len(data) / 1e6:.0f} MB (built in {built:.0f}s), {chunks} chunks")
    for stage in sorted(stages, key=lambda s: s.started_at or s.finished_at or 0):
        if stage.started_at and stage.finished_at:
            print(f"  {stage.stage:14} {(stage.finished_at - stage.started_at).total_seconds():7.1f}s")
    print(f"  upload to review   {to_review:7.1f}s")
    print(f"  confirm to ready   {finished - indexing:7.1f}s")
    print(f"  total              {to_review + finished - indexing:7.1f}s ({pages / (to_review + finished - indexing):.0f} pages/s)")
