import uuid
from typing import BinaryIO

import logging

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationError,
)
from app.infrastructure.queue.base import Queue
from app.infrastructure.storage.base import Storage
from app.modules.audit.service import record_event
from app.modules.auth.acl import (
    can_see,
    default_write_scope,
    ensure_can_write_scope,
)
from app.modules.auth.permissions import Permission, Principal
from app.modules.auth.scope import tenant_id
from app.modules.branches.repository import BranchRepository
from app.modules.categories.repository import CategoryRepository
from app.modules.departments.repository import DepartmentRepository
from app.modules.documents.model import (
    Document,
    DocumentPage,
    DocumentStatus,
    ExtractionMethod,
)
from app.modules.documents.repository import DocumentRepository
from app.modules.documents.repository import ACTIVE_STATUSES
from app.modules.documents.schema import DocumentUpdate, DuplicateInfo, DuplicateResult
from app.modules.documents.scope_sync import sync_document_scope
from app.modules.ingestion import progress
from app.modules.ingestion.model import Stage, StageStatus
from app.modules.ingestion.pipeline import enqueue_inspection, enqueue_resume
from app.modules.organizations.repository import bump_knowledge_version
from app.modules.policies.model import Policy, PolicyVersion, VersionStatus
from app.modules.versions.timeline import refresh_change_summaries, withdraw_version

logger = logging.getLogger(__name__)

PDF_MAGIC = b"%PDF-"
MIN_DUPLICATE_REASON = 5


class _LimitedReader:
    """Streams an upload while enforcing the size limit without buffering it."""

    def __init__(self, source: BinaryIO, max_bytes: int) -> None:
        self.source = source
        self.max_bytes = max_bytes
        self.read_bytes = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self.source.read(size)
        self.read_bytes += len(chunk)
        if self.read_bytes > self.max_bytes:
            raise PayloadTooLargeError(
                f"File exceeds the maximum upload size of {self.max_bytes // (1024 * 1024)} MB."
            )
        return chunk


class DocumentService:
    def __init__(self, session: Session, storage: Storage, queue: Queue, settings: Settings) -> None:
        self.session = session
        self.storage = storage
        self.queue = queue
        self.settings = settings
        self.repository = DocumentRepository(session)
        self.branches = BranchRepository(session)
        self.departments = DepartmentRepository(session)
        self.categories = CategoryRepository(session)

    # --- scope ---------------------------------------------------------------

    def _resolve_scope(
        self,
        principal: Principal,
        branch_id: uuid.UUID | None,
        department_id: uuid.UUID | None,
        *,
        use_default: bool,
    ) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        organization_id = tenant_id(principal)
        if use_default and branch_id is None and department_id is None:
            branch_id, department_id = default_write_scope(principal)
        if department_id is not None:
            department = self.departments.get_in_org(organization_id, department_id)
            if department is None:
                raise ValidationError("Department not found.")
            if branch_id is None:
                branch_id = department.branch_id
            elif department.branch_id != branch_id:
                raise ValidationError("Department does not belong to the selected branch.")
        if branch_id is not None and self.branches.get_in_org(organization_id, branch_id) is None:
            raise ValidationError("Branch not found.")
        ensure_can_write_scope(principal, branch_id, department_id)
        return branch_id, department_id

    # --- upload ----------------------------------------------------------------

    def upload(
        self,
        principal: Principal,
        *,
        file: BinaryIO,
        filename: str,
        content_type: str | None,
        branch_id: uuid.UUID | None,
        department_id: uuid.UUID | None,
        category_id: uuid.UUID | None,
        allow_duplicate: bool,
        duplicate_reason: str | None,
    ) -> Document:
        organization_id = tenant_id(principal)
        branch_id, department_id = self._resolve_scope(
            principal, branch_id, department_id, use_default=True
        )
        if category_id is not None and self.categories.get_in_org(organization_id, category_id) is None:
            raise ValidationError("Category not found.")

        safe_name = (filename or "document.pdf").replace("/", "_").replace("\\", "_")[:500]
        if not safe_name.lower().endswith(".pdf"):
            raise ValidationError("Only PDF documents are supported.", code="UNSUPPORTED_FILE_TYPE")
        head = file.read(1024)
        if PDF_MAGIC not in head:
            raise ValidationError("The file is not a valid PDF.", code="UNSUPPORTED_FILE_TYPE")
        file.seek(0)

        document_id = uuid.uuid4()
        key = f"originals/{organization_id}/{document_id}.pdf"
        max_bytes = self.settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        stored = self.storage.put(key, _LimitedReader(file, max_bytes))
        if stored.size == 0:
            self.storage.delete(key)
            raise ValidationError("The file is empty.")

        try:
            existing = self.repository.find_by_file_hash(organization_id, stored.sha256)
            if existing is not None and not allow_duplicate:
                raise self._duplicate_error(principal, existing)
            if existing is not None and (
                not duplicate_reason or len(duplicate_reason.strip()) < MIN_DUPLICATE_REASON
            ):
                raise ValidationError(
                    "A reason is required to upload a duplicate document.",
                    code="DUPLICATE_REASON_REQUIRED",
                )

            document = Document(
                id=document_id,
                organization_id=organization_id,
                branch_id=branch_id,
                department_id=department_id,
                category_id=category_id,
                original_filename=safe_name,
                storage_key=key,
                content_type="application/pdf",
                size_bytes=stored.size,
                file_sha256=stored.sha256,
                status=DocumentStatus.UPLOADED,
                uploaded_by_id=principal.user_id,
                duplicate_of_id=existing.id if existing else None,
                duplicate_override_reason=duplicate_reason.strip() if existing else None,
            )
            self.session.add(document)
            self.session.flush()
            progress.init_stages(self.session, document.id)
            progress.finish(self.session, document.id, Stage.UPLOAD, detail={"bytes": stored.size})
            progress.finish(
                self.session, document.id, Stage.VALIDATION,
                detail={"sha256": stored.sha256, "pdf_header": True},
            )
            record_event(
                self.session, "document.uploaded", actor=principal, resource_type="document",
                resource_id=document.id,
                details={"filename": safe_name, "size_bytes": stored.size, "sha256": stored.sha256},
            )
            if existing is not None:
                record_event(
                    self.session, "document.duplicate_override", actor=principal,
                    resource_type="document", resource_id=document.id,
                    details={"duplicate_of": str(existing.id), "reason": document.duplicate_override_reason},
                )
            enqueue_inspection(self.queue, self.session, document)
            self.session.commit()
        except BaseException:
            self.session.rollback()
            self.storage.delete(key)
            raise
        return document

    def check_duplicates(self, principal: Principal, hashes: list[str]) -> list[DuplicateResult]:
        """Before uploading: which of these files (by SHA-256) are already in the organisation."""
        wanted = list(dict.fromkeys(h.lower() for h in hashes))
        existing: dict[str, Document] = {}
        for document in self.session.scalars(
            select(Document).where(
                Document.organization_id == tenant_id(principal), Document.file_sha256.in_(wanted),
                Document.status.in_(ACTIVE_STATUSES),
            ).order_by(Document.created_at)
        ):
            existing.setdefault(document.file_sha256, document)
        results = []
        for sha in wanted:
            match = existing.get(sha)
            if match is None:
                results.append(DuplicateResult(sha256=sha, duplicate=False))
            elif not can_see(principal, match.organization_id, match.branch_id, match.department_id, match.policy_id):
                results.append(DuplicateResult(sha256=sha, duplicate=True, restricted=True))
            else:
                results.append(DuplicateResult(sha256=sha, duplicate=True, existing=DuplicateInfo(
                    document_id=match.id, title=match.title, original_filename=match.original_filename,
                    uploaded_at=match.created_at, policy_id=match.policy_id, policy_version_id=match.policy_version_id,
                )))
        return results

    def _duplicate_error(self, principal: Principal, existing: Document) -> ConflictError:
        if not can_see(principal, existing.organization_id, existing.branch_id, existing.department_id, existing.policy_id):
            # Do not reveal anything about documents the uploader cannot access.
            return ConflictError(
                "An identical document already exists in an area you cannot access. "
                "Contact an administrator.",
                code="DUPLICATE_DOCUMENT",
            )
        info = DuplicateInfo(
            document_id=existing.id,
            title=existing.title,
            original_filename=existing.original_filename,
            uploaded_at=existing.created_at,
            policy_id=existing.policy_id,
            policy_version_id=existing.policy_version_id,
        )
        return ConflictError(
            "This document has already been uploaded.",
            code="DUPLICATE_DOCUMENT",
            details={"match": "sha256", "existing": info.model_dump(mode="json")},
        )

    # --- reads -----------------------------------------------------------------

    def get(self, principal: Principal, document_id: uuid.UUID) -> Document:
        document = self.repository.get_visible(principal, document_id)
        if document is None:
            raise NotFoundError("Document not found.")
        return document

    def progress(self, principal: Principal, document_id: uuid.UUID) -> dict:
        document = self.get(principal, document_id)
        return progress.snapshot(self.session, document.id)

    def open_file(self, principal: Principal, document_id: uuid.UUID):
        document = self.get(principal, document_id)
        record_event(
            self.session, "document.accessed", actor=principal, resource_type="document",
            resource_id=document.id,
        )
        self.session.commit()
        return document, self.storage.open(document.storage_key)

    # --- writes ----------------------------------------------------------------

    def _get_manageable(self, principal: Principal, document_id: uuid.UUID) -> Document:
        document = self.get(principal, document_id)
        if not principal.has(Permission.DOCUMENTS_MANAGE) and document.uploaded_by_id != principal.user_id:
            raise NotFoundError("Document not found.")
        ensure_can_write_scope(principal, document.branch_id, document.department_id)
        return document

    def update(self, principal: Principal, document_id: uuid.UUID, data: DocumentUpdate) -> Document:
        document = self._get_manageable(principal, document_id)
        changes = data.model_dump(exclude_unset=True)
        if "branch_id" in changes or "department_id" in changes:
            document.branch_id, document.department_id = self._resolve_scope(
                principal,
                changes.get("branch_id", document.branch_id),
                changes.get("department_id", document.department_id),
                use_default=False,
            )
        if changes.get("category_id") is not None:
            if self.categories.get_in_org(document.organization_id, changes["category_id"]) is None:
                raise ValidationError("Category not found.")
            document.category_id = changes["category_id"]
        if document.status == DocumentStatus.READY and {"branch_id", "department_id", "category_id"} & changes.keys():
            sync_document_scope(self.session, document)
            bump_knowledge_version(self.session, document.organization_id)
        if changes.get("title"):
            document.title = changes["title"]
        record_event(
            self.session, "document.updated", actor=principal, resource_type="document",
            resource_id=document.id, details={"changes": {k: str(v) for k, v in changes.items()}},
        )
        self.session.commit()
        return document

    def archive(self, principal: Principal, document_id: uuid.UUID) -> Document:
        document = self._get_manageable(principal, document_id)
        if document.status == DocumentStatus.ARCHIVED:
            return document
        was_searchable = document.status == DocumentStatus.READY
        document.status = DocumentStatus.ARCHIVED
        if was_searchable:
            bump_knowledge_version(self.session, document.organization_id)
        record_event(
            self.session, "document.archived", actor=principal, resource_type="document",
            resource_id=document.id,
        )
        self.session.commit()
        return document

    def delete(self, principal: Principal, document_id: uuid.UUID) -> None:
        """Delete a document and everything derived from it, for good.

        Its version leaves the policy's timeline the way a withdrawn one does (the
        previous version runs on until the next), then is deleted; a policy left
        with no versions is deleted too. Pages, sections, chunks and analyses go
        with the document (database cascades), as do its pending pipeline jobs,
        notifications that link to it, and the stored file. The audit log keeps a
        record of what was deleted.
        """
        document = self._get_manageable(principal, document_id)
        if document.status in (DocumentStatus.UPLOADED, DocumentStatus.PROCESSING, DocumentStatus.INDEXING):
            raise ConflictError(
                "This document is still being processed. Delete it once processing finishes or fails.",
                code="INVALID_STATE",
            )
        version = self.session.scalar(select(PolicyVersion).where(PolicyVersion.document_id == document.id))
        policy = self.session.get(Policy, version.policy_id if version else document.policy_id) if (
            version or document.policy_id) else None
        details = {
            "filename": document.original_filename, "title": document.title, "status": document.status,
            "policy_id": str(policy.id) if policy else None, "policy": policy.name if policy else None,
            "version": version.version_label if version else None,
        }

        if version is not None:
            if version.status == VersionStatus.ACTIVE:
                following_id = version.superseded_by_version_id
                withdraw_version(self.session, version, "Document deleted")
                refresh_change_summaries(self.session, self.session.get(PolicyVersion, following_id) if following_id else None)
            document.policy_version_id = None
            self.session.flush()
            self.session.delete(version)
            self.session.flush()

        key, organization_id, was_searchable = document.storage_key, document.organization_id, document.status == DocumentStatus.READY
        ids = {"id": str(document.id)}
        self.session.execute(
            text("DELETE FROM queue_jobs WHERE status IN ('queued', 'failed') AND payload->>'document_id' = :id"), ids,
        )
        self.session.execute(
            text("DELETE FROM notifications WHERE data->>'document_id' = :id OR link LIKE '/documents/' || :id || '%'"), ids,
        )
        self.session.delete(document)
        self.session.flush()

        if policy is not None and not self.session.scalar(
            select(func.count()).select_from(PolicyVersion).where(PolicyVersion.policy_id == policy.id)
        ):
            self.session.execute(
                text("DELETE FROM notifications WHERE data->>'policy_id' = :id OR link LIKE '/policies/' || :id || '%'"),
                {"id": str(policy.id)},
            )
            self.session.execute(delete(Policy).where(Policy.id == policy.id))
            details["policy_deleted"] = True

        if was_searchable or version is not None:
            bump_knowledge_version(self.session, organization_id)  # cached answers may cite it
        record_event(
            self.session, "document.deleted", actor=principal, resource_type="document",
            resource_id=document_id, details=details,
        )
        self.session.commit()
        try:
            self.storage.delete(key)
        except Exception:  # the record is gone either way; an orphaned file is only wasted space
            logger.warning("Could not delete the stored file %s", key, exc_info=True)

    def retry(self, principal: Principal, document_id: uuid.UUID) -> Document:
        document = self._get_manageable(principal, document_id)
        if document.status != DocumentStatus.FAILED:
            raise ConflictError("Only failed documents can be retried.", code="INVALID_STATE")
        requeue_failed(self.session, self.queue, document, actor=principal)
        self.session.commit()
        return document


def requeue_failed(session: Session, queue: Queue, document: Document, *, actor: Principal | None) -> Stage:
    """Resume a failed document from the stage that failed. The caller commits."""
    error = document.error or {}
    failed_stage = error.get("stage", Stage.EXTRACTION)
    if error.get("code") == "OCR_UNAVAILABLE":
        # OCR may have been installed since: queue those pages for OCR again.
        # Reset both OCR_UNAVAILABLE (legacy) and PENDING_OCR (current) pages.
        session.execute(
            update(DocumentPage)
            .where(
                DocumentPage.document_id == document.id,
                DocumentPage.method.in_((ExtractionMethod.OCR_UNAVAILABLE, ExtractionMethod.PENDING_OCR)),
            )
            .values(method=ExtractionMethod.PENDING_OCR)
        )
        failed_stage = Stage.EXTRACTION
    document.status = DocumentStatus.UPLOADED
    document.error = None
    document.ingestion_attempt += 1
    progress.reset_from(session, document.id, Stage(failed_stage))
    record_event(
        session, "document.retry", actor=actor, organization_id=document.organization_id,
        resource_type="document", resource_id=document.id,
        details={"from_stage": failed_stage, "automatic": actor is None, "previous_error": error.get("code")},
    )
    enqueue_resume(queue, session, document, Stage(failed_stage))
    return Stage(failed_stage)


def mark_failed(session: Session, document: Document, stage: Stage, code: str, message: str) -> None:
    document.status = DocumentStatus.FAILED
    document.error = {"stage": stage, "code": code, "message": message}
    progress.finish(session, document.id, stage, status=StageStatus.FAILED, detail={"error": message})
