"""Turning a reviewed suggestion into confirmed policy data.

Nothing becomes an identity, version or relationship until a person with
policies:manage confirms it here. The reviewer may override every suggestion;
overriding a duplicate/conflict warning requires a recorded reason.
"""

import uuid
from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import translate_unique_violation
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.infrastructure.queue.base import Queue
from app.modules.audit.service import record_event
from app.modules.auth.acl import can_see, ensure_can_write_scope
from app.modules.auth.permissions import Principal
from app.modules.auth.scope import tenant_id
from app.modules.branches.repository import BranchRepository
from app.modules.categories.repository import CategoryRepository
from app.modules.departments.repository import DepartmentRepository
from app.modules.documents.model import Document, DocumentStatus
from app.modules.documents.repository import DocumentRepository
from app.modules.ingestion import pipeline, progress
from app.modules.ingestion.analysis.metadata import normalize_title
from app.modules.ingestion.model import Decision, DocumentAnalysis, ReviewStatus, Stage
from app.modules.ingestion.schema import PolicyInput, RelationshipInput, VersionInput
from app.modules.policies.model import (
    DateSource,
    DocumentRelationship,
    Policy,
    PolicyStatus,
    PolicyVersion,
    RelationStatus,
    RelationType,
    VersionStatus,
)
from app.modules.versions.timeline import insert_version, refresh_change_summaries

WARNING_DECISIONS = {Decision.EXACT_DUPLICATE, Decision.CONTENT_DUPLICATE, Decision.VERSION_CONFLICT}
MIN_REASON = 5


class ConfirmRequest(BaseModel):
    action: Literal["create_policy", "add_version", "reject"]
    policy: PolicyInput | None = None
    policy_id: uuid.UUID | None = None
    version: VersionInput | None = None
    relationships: list[RelationshipInput] = Field(default_factory=list, max_length=50)
    conflict_resolution: Literal["save_as_new_revision"] | None = None
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def check_shape(self) -> "ConfirmRequest":
        if self.action == "create_policy" and (self.policy is None or self.version is None):
            raise ValueError("create_policy requires policy and version.")
        if self.action == "add_version" and (self.policy_id is None or self.version is None):
            raise ValueError("add_version requires policy_id and version.")
        return self


class ConfirmationService:
    def __init__(self, session: Session, queue: Queue) -> None:
        self.session = session
        self.queue = queue
        self.documents = DocumentRepository(session)
        self.categories = CategoryRepository(session)
        self.branches = BranchRepository(session)
        self.departments = DepartmentRepository(session)

    def confirm(
        self,
        principal: Principal,
        document_id: uuid.UUID,
        request: ConfirmRequest,
        *,
        effective_date_source: str | None = None,
        commit: bool = True,
    ) -> Document:
        """Apply a reviewed decision.

        `effective_date_source` records where a supplied date came from when it
        was not typed by a person (bulk uploads order undated versions). With
        `commit=False` the caller owns the transaction (bulk confirmation runs
        each version in a savepoint).
        """
        document = self.documents.get_visible(principal, document_id)
        if document is None:
            raise NotFoundError("Document not found.")
        document = self.session.get(Document, document_id, with_for_update=True, populate_existing=True)
        analysis = self.session.get(DocumentAnalysis, document_id, with_for_update=True, populate_existing=True)
        if document.status != DocumentStatus.AWAITING_CONFIRMATION or analysis is None:
            raise ConflictError("This document is not awaiting confirmation.", code="INVALID_STATE")
        if analysis.review_status != ReviewStatus.PENDING:
            raise ConflictError("This analysis has already been reviewed.", code="INVALID_STATE")

        # A reason given when the duplicate was uploaded anyway still stands.
        reason = (request.reason or "").strip() or (document.duplicate_override_reason or "").strip() or None
        if request.action == "reject":
            return self._reject(principal, document, analysis, reason)
        if self._reissue(analysis, request):
            # A new version of the very policy the copy belongs to is what a reissue is:
            # confirming it is the decision, recorded with the reviewer in the audit log.
            reason = reason or "New version of the same policy (reissue), confirmed by the reviewer"
            if not request.conflict_resolution:
                # Same version label as the original: a reissue is a revision of it.
                request = request.model_copy(update={"conflict_resolution": "save_as_new_revision"})
        elif analysis.decision in WARNING_DECISIONS and (not reason or len(reason) < MIN_REASON):
            raise ValidationError(
                f"The analysis flagged this upload as {analysis.decision}. "
                f"Give a short reason (at least {MIN_REASON} characters) to proceed.",
                code="OVERRIDE_REASON_REQUIRED",
            )

        if request.action == "create_policy":
            policy = self._create_policy(principal, document, request.policy)
            version_number = 1
        else:
            policy = self._manageable_policy(principal, request.policy_id)
            version_number = (self.session.scalar(
                select(func.max(PolicyVersion.version_number)).where(PolicyVersion.policy_id == policy.id)
            ) or 0) + 1

        version = self._build_version(principal, document, analysis, policy, request, version_number, effective_date_source)
        previous, following = insert_version(self.session, policy, version)
        refresh_change_summaries(self.session, version, following)

        # The document now lives at the policy's scope and carries its confirmed identity.
        document.branch_id, document.department_id = policy.branch_id, policy.department_id
        document.category_id = policy.category_id
        document.title = policy.name
        document.policy_id, document.policy_version_id = policy.id, version.id
        document.status = DocumentStatus.INDEXING

        relationships = [self._relationship(principal, document, r) for r in request.relationships]

        analysis.review_status = ReviewStatus.CONFIRMED
        analysis.reviewed_by_id = principal.user_id
        analysis.reviewed_at = datetime.now(UTC)
        analysis.resolution = request.model_dump(mode="json")
        progress.finish(self.session, document.id, Stage.CONFIRMATION, detail={
            "action": request.action, "policy_id": str(policy.id), "version_id": str(version.id),
        })
        record_event(
            self.session, "document.confirmed", actor=principal, resource_type="document",
            resource_id=document.id,
            details={
                "action": request.action,
                "suggested_decision": analysis.decision,
                "policy_id": str(policy.id),
                "version_id": str(version.id),
                "version_label": version.version_label,
                "effective_from": version.effective_from.isoformat(),
                "effective_date_source": version.effective_date_source,
                "override_reason": reason if analysis.decision in WARNING_DECISIONS else None,
                "followed_suggestion": self._followed(analysis, request, policy),
            },
        )
        record_event(
            self.session, "policy_version.created", actor=principal, resource_type="policy_version",
            resource_id=version.id,
            details={"policy_id": str(policy.id), "label": version.version_label,
                     "supersedes": str(previous.id) if previous else None},
        )
        for relationship in relationships:
            record_event(
                self.session, "relationship.created", actor=principal, resource_type="relationship",
                resource_id=relationship.id,
                details={"type": relationship.relation_type, "target_policy_id": str(relationship.target_policy_id)},
            )
        pipeline.enqueue_step(self.queue, self.session, document, pipeline.CHUNK, "chunk")
        from app.modules.uploads.hooks import document_confirmed

        document_confirmed(self.session, document)
        if commit:
            self.session.commit()
        return document

    # --- helpers -----------------------------------------------------------------

    @staticmethod
    def _reissue(analysis, request) -> bool:
        """Adding a (near-)copy as a new version of the policy its original belongs to."""
        existing = (analysis.conflict or {}).get("existing_policy_id")
        return (
            analysis.decision in (Decision.EXACT_DUPLICATE, Decision.CONTENT_DUPLICATE)
            and request.action == "add_version"
            and existing is not None and str(request.policy_id) == existing
        )

    def _reject(self, principal, document, analysis, reason) -> Document:
        document.status = DocumentStatus.REJECTED
        analysis.review_status = ReviewStatus.REJECTED
        analysis.reviewed_by_id = principal.user_id
        analysis.reviewed_at = datetime.now(UTC)
        analysis.resolution = {"action": "reject", "reason": reason}
        progress.finish(self.session, document.id, Stage.CONFIRMATION, detail={"action": "reject"})
        record_event(
            self.session, "document.rejected", actor=principal, resource_type="document",
            resource_id=document.id, details={"reason": reason, "suggested_decision": analysis.decision},
        )
        self.session.commit()
        return document

    def _create_policy(self, principal: Principal, document: Document, data: PolicyInput) -> Policy:
        organization_id = tenant_id(principal)
        category = self.categories.get_in_org(organization_id, data.category_id)
        if category is None or not category.is_active:
            raise ValidationError("Category not found.")
        branch_id = data.branch_id if data.branch_id or data.department_id else document.branch_id
        department_id = data.department_id if data.branch_id or data.department_id else document.department_id
        if department_id is not None:
            department = self.departments.get_in_org(organization_id, department_id)
            if department is None or (branch_id and department.branch_id != branch_id):
                raise ValidationError("Department not found in the selected branch.")
            branch_id = department.branch_id
        if branch_id is not None and self.branches.get_in_org(organization_id, branch_id) is None:
            raise ValidationError("Branch not found.")
        ensure_can_write_scope(principal, branch_id, department_id)

        name = " ".join(data.name.split())
        if not normalize_title(name):
            raise ValidationError("Policy name is required.")
        policy = Policy(
            organization_id=organization_id, branch_id=branch_id, department_id=department_id,
            category_id=category.id, name=name[:500], normalized_name=normalize_title(name)[:500],
            policy_number=(data.policy_number or "").strip().upper() or None,
            document_number=(data.document_number or "").strip().upper() or None,
            issuer=data.issuer, issuing_department=data.issuing_department, owner=data.owner,
            description=data.description, created_by_id=principal.user_id,
        )
        with translate_unique_violation(self.session, "A policy with this policy number already exists.", "POLICY_NUMBER_EXISTS"):
            self.session.add(policy)
            self.session.flush()
        record_event(
            self.session, "policy.created", actor=principal, resource_type="policy", resource_id=policy.id,
            details={"name": policy.name, "policy_number": policy.policy_number, "category": category.name},
        )
        return policy

    def _manageable_policy(self, principal: Principal, policy_id: uuid.UUID) -> Policy:
        policy = self.session.get(Policy, policy_id)
        if policy is None or not can_see(principal, policy.organization_id, policy.branch_id, policy.department_id, policy.id):
            raise NotFoundError("Policy not found.")
        if policy.status != PolicyStatus.ACTIVE:
            raise ConflictError("The policy is archived.", code="INVALID_STATE")
        ensure_can_write_scope(principal, policy.branch_id, policy.department_id)
        return policy

    def _build_version(
        self, principal: Principal, document: Document, analysis: DocumentAnalysis, policy: Policy,
        request: ConfirmRequest, number: int, date_source: str | None,
    ) -> PolicyVersion:
        data: VersionInput = request.version
        if data.effective_from is not None:
            effective_from, date_source = data.effective_from, date_source or DateSource.ENTERED
        else:
            effective_from, date_source = default_effective_date(document, analysis)
        # Nothing is asked of the reviewer: the label comes from the document itself,
        # and the running version number is the last resort.
        label_auto = not (data.version_label or "").strip()
        detected_label = ((analysis.detected or {}).get("version_label") or {}).get("value")
        label = (data.version_label or detected_label or str(number)).strip()[:50]
        revision = data.revision_number or 0
        existing = self.session.scalars(
            select(PolicyVersion).where(PolicyVersion.policy_id == policy.id, PolicyVersion.status == VersionStatus.ACTIVE)
        ).all()
        if document.content_hash and (same := next((v for v in existing if v.content_hash == document.content_hash), None)):
            raise ConflictError(
                f"Version {same.version_label} has identical content.",
                code="CONTENT_DUPLICATE", details={"existing_version_id": str(same.id)},
            )
        same_label = [v for v in existing if v.version_label == label and v.revision_number == revision]
        if same_label:
            if request.conflict_resolution != "save_as_new_revision":
                raise ConflictError(
                    f"Version {label} already exists with different content.",
                    code="VERSION_CONFLICT",
                    details={"type": "VERSION_LABEL_EXISTS", "existing_version_id": str(same_label[0].id),
                             "options": ["save_as_new_revision", "change_version_label", "reject"]},
                )
            revision = max(v.revision_number for v in existing if v.version_label == label) + 1
        return PolicyVersion(
            id=uuid.uuid4(), organization_id=policy.organization_id, policy_id=policy.id,
            document_id=document.id, version_number=number, version_label=label, version_label_auto=label_auto,
            revision_number=revision, effective_from=effective_from, effective_to=data.effective_to,
            effective_date_source=date_source,
            content_hash=document.content_hash, created_by_id=principal.user_id,
            confirmed_at=datetime.now(UTC),
        )

    def _relationship(self, principal: Principal, document: Document, data: RelationshipInput) -> DocumentRelationship:
        try:
            relation = RelationType(data.relation_type.upper())
        except ValueError:
            raise ValidationError(f"Unknown relation type {data.relation_type}.") from None
        target = self.session.get(Policy, data.target_policy_id)
        if target is None or not can_see(principal, target.organization_id, target.branch_id, target.department_id, target.id):
            raise NotFoundError("Related policy not found.")
        if target.id == document.policy_id:
            raise ValidationError("A document cannot relate to its own policy.")
        if data.target_version_id:
            version = self.session.get(PolicyVersion, data.target_version_id)
            if version is None or version.policy_id != target.id:
                raise ValidationError("Target version does not belong to the related policy.")
        relationship = DocumentRelationship(
            id=uuid.uuid4(), organization_id=document.organization_id, source_document_id=document.id,
            relation_type=relation, target_policy_id=target.id, target_version_id=data.target_version_id,
            clauses=data.clauses, evidence=data.evidence, status=RelationStatus.CONFIRMED,
            created_by_id=principal.user_id,
        )
        self.session.add(relationship)
        self.session.flush()
        return relationship

    @staticmethod
    def _followed(analysis: DocumentAnalysis, request: ConfirmRequest, policy: Policy) -> bool:
        if request.action == "create_policy":
            return analysis.decision in (Decision.NEW_POLICY, Decision.EXISTING_POLICY_AMENDMENT)
        return analysis.matched_policy_id == policy.id


def detected_effective_date(analysis: DocumentAnalysis | None) -> date | None:
    """The effective (or, failing that, issue) date the document itself states."""
    detected = (analysis.detected or {}) if analysis is not None else {}
    for key in ("effective_date", "issue_date"):
        if value := detected.get(key):
            try:
                return date.fromisoformat(value)
            except (TypeError, ValueError):
                continue
    return None


def default_effective_date(document: Document, analysis: DocumentAnalysis | None) -> tuple[date, str]:
    """The date used when none is entered: the document's own, else its upload date."""
    if (stated := detected_effective_date(analysis)) is not None:
        return stated, DateSource.DETECTED
    uploaded = document.created_at.date() if document.created_at else datetime.now(UTC).date()
    return uploaded, DateSource.UPLOAD_DATE
