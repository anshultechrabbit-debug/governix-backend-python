"""Keep a bulk upload's plan in step with its documents.

    document_confirmed   inside a confirmation (same transaction): the item is done,
                         whether the batch confirmed it or a person did after review
    document_settled     after analysis finished or the document failed: its group
                         may now be complete and ready to confirm
"""

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.infrastructure.queue.base import Queue
from app.modules.documents.model import Document
from app.modules.uploads.model import ItemStatus, UploadBatchGroup, UploadBatchItem

logger = logging.getLogger(__name__)


def document_confirmed(session: Session, document: Document) -> None:
    item = session.scalar(select(UploadBatchItem).where(UploadBatchItem.document_id == document.id))
    if item is None:
        return
    if item.status == ItemStatus.NEEDS_REVIEW:
        item.message = "Confirmed after review."
    item.status, item.policy_version_id = ItemStatus.CONFIRMED, document.policy_version_id
    group = session.get(UploadBatchGroup, item.group_id)
    if group is not None and group.policy_id is None:
        # Confirmed by a person: the group's other files are now offered as versions of this policy.
        group.policy_id = document.policy_id
    session.flush()
    from app.modules.uploads.service import refresh_statuses

    refresh_statuses(session, item.batch_id)


def document_settled(session_factory: sessionmaker[Session], queue: Queue, document_id: uuid.UUID) -> None:
    """Never raises: a bulk upload that cannot advance is visible on its page and can be resumed."""
    from app.modules.uploads.service import advance_for_document

    try:
        advance_for_document(session_factory, queue, document_id)
    except Exception:  # noqa: BLE001
        logger.warning("Could not advance the bulk upload of document %s", document_id, exc_info=True)
