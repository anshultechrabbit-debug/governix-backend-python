"""Server-side page rendering for the document viewer, with citation highlighting.

Pages are rendered one at a time from the stored PDF (never the whole file) and
the cited chunk's sentences are highlighted where PyMuPDF can locate them.
"""

import uuid

import pymupdf
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError, ValidationError
from app.infrastructure.storage.base import Storage
from app.modules.documents.model import Document, DocumentPage, DocumentSection
from app.modules.ingestion.text import split_sentences
from app.modules.search.model import Chunk

MAX_ZOOM = 3.0
HIGHLIGHT_FILL = (1.0, 0.85, 0.1)
SEARCH_FRAGMENT = 60


def _fragments(text: str) -> list[str]:
    """Short, line-independent pieces of the chunk that page.search_for can find."""
    fragments = []
    for sentence in split_sentences(text):
        words = sentence.split()
        for start in range(0, len(words), 8):
            piece = " ".join(words[start:start + 8])
            if len(piece) >= 12:
                fragments.append(piece[:SEARCH_FRAGMENT])
    return fragments[:60]


def render_page(
    session: Session,
    storage: Storage,
    document: Document,
    page_number: int,
    *,
    zoom: float,
    highlight_chunk_id: uuid.UUID | None,
) -> tuple[bytes, int]:
    if not document.page_count or not 1 <= page_number <= document.page_count:
        raise NotFoundError("Page not found.")
    if not 0.25 <= zoom <= MAX_ZOOM:
        raise ValidationError(f"zoom must be between 0.25 and {MAX_ZOOM}.")
    highlight_text = None
    if highlight_chunk_id is not None:
        chunk = session.get(Chunk, highlight_chunk_id)
        if chunk is None or chunk.document_id != document.id:
            raise NotFoundError("Highlight not found.")
        if chunk.page_start <= page_number <= chunk.page_end:
            highlight_text = chunk.text

    highlights = 0
    with storage.local_path(document.storage_key) as path:
        pdf = pymupdf.open(path)
        try:
            page = pdf.load_page(page_number - 1)
            if highlight_text:
                for fragment in _fragments(highlight_text):
                    for rect in page.search_for(fragment, quads=False):
                        page.draw_rect(rect, color=None, fill=HIGHLIGHT_FILL, fill_opacity=0.35, overlay=True)
                        highlights += 1
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            return pixmap.tobytes("png"), highlights
        finally:
            pdf.close()


def page_text(session: Session, document: Document, page_number: int) -> DocumentPage:
    page = session.get(DocumentPage, (document.id, page_number))
    if page is None:
        raise NotFoundError("Page not found.")
    return page


def outline(session: Session, document: Document, limit: int = 2000) -> list[DocumentSection]:
    return list(session.scalars(
        select(DocumentSection)
        .where(DocumentSection.document_id == document.id, DocumentSection.level > 0)
        .order_by(DocumentSection.order_index)
        .limit(limit)
    ))
