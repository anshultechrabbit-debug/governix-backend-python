from sqlalchemy import update
from sqlalchemy.orm import Session

from app.modules.documents.model import Document
from app.modules.search.model import Chunk


def sync_document_scope(session: Session, document: Document) -> None:
    """Chunks carry a copy of the document's ACL scope for in-index filtering; keep it in step."""
    session.execute(
        update(Chunk)
        .where(Chunk.document_id == document.id)
        .values(branch_id=document.branch_id, department_id=document.department_id,
                category_id=document.category_id)
    )
