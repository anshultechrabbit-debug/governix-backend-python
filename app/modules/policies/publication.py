"""A version's document became searchable: announce it and summarise it."""

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.infrastructure.queue.base import Queue
from app.modules.documents.model import Document
from app.modules.notifications import service as notifications
from app.modules.policies.model import Policy, PolicyVersion, VersionStatus
from app.modules.versions.summary import SUMMARIZE


def document_published(session: Session, queue: Queue, document: Document) -> None:
    """Call in the transaction that marks the document READY."""
    if document.policy_version_id is None:
        return
    version = session.get(PolicyVersion, document.policy_version_id)
    if version is None or version.status != VersionStatus.ACTIVE:
        return
    policy = session.get(Policy, version.policy_id)
    notifications.version_published(
        session, policy, version, today=datetime.now(UTC).date(), actor_id=version.created_by_id,
    )
    queue.enqueue(
        SUMMARIZE, {"version_id": str(version.id)},
        idempotency_key=f"summary:{version.id}:{document.ingestion_attempt}",
        organization_id=document.organization_id, session=session,
    )
