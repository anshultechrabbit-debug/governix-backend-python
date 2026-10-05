import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.modules.auth.acl import can_see
from app.modules.auth.permissions import Principal
from app.modules.categories.model import Category
from app.modules.documents.model import Document
from app.modules.documents.repository import DocumentRepository
from app.modules.ingestion.model import Decision, DocumentAnalysis
from app.modules.ingestion.schema import AnalysisRead
from app.modules.policies.model import Policy, PolicyVersion, VersionStatus

RESTRICTED = {"restricted": True, "message": "Matches a document in an area you cannot access."}


# Decisions that say the upload copies a document already filed.
DUPLICATE_DECISIONS = {"EXACT_DUPLICATE", "CONTENT_DUPLICATE"}


class AnalysisService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.documents = DocumentRepository(session)

    def _visible_policy(self, principal: Principal, policy_id: str | uuid.UUID | None) -> Policy | None:
        if not policy_id:
            return None
        policy = self.session.get(Policy, uuid.UUID(str(policy_id)))
        if policy and can_see(principal, policy.organization_id, policy.branch_id, policy.department_id, policy.id):
            return policy
        return None

    def get(self, principal: Principal, document_id: uuid.UUID) -> AnalysisRead:
        document = self.documents.get_visible(principal, document_id)
        analysis = self.session.get(DocumentAnalysis, document_id) if document else None
        if analysis is None:
            raise NotFoundError("Analysis not available yet.")

        matched = None
        if analysis.matched_policy_id:
            policy = self._visible_policy(principal, analysis.matched_policy_id)
            matched = self._policy_summary(policy) if policy else RESTRICTED
        elif related := self._related_policy_id(analysis):
            policy = self._visible_policy(principal, related)
            matched = self._policy_summary(policy) if policy else None

        candidates = [
            c if self._visible_policy(principal, c["policy_id"]) else {**RESTRICTED, "score": c["score"]}
            for c in analysis.candidates
        ]
        targets = [t for t in analysis.amendment_targets if self._visible_policy(principal, t["policy_id"])]
        conflict = analysis.conflict
        if conflict and conflict.get("existing_document_id"):
            existing = self.documents.get_visible(principal, uuid.UUID(conflict["existing_document_id"]))
            if existing is None:
                conflict = {"type": conflict["type"], "match": conflict.get("match"), **RESTRICTED}

        category = (
            self.session.get(Category, analysis.suggested_category_id)
            if analysis.suggested_category_id else None
        )
        return AnalysisRead(
            document_id=analysis.document_id,
            detected=analysis.detected,
            suggested_name=analysis.suggested_name,
            name_confidence=analysis.name_confidence,
            suggested_category_id=analysis.suggested_category_id,
            suggested_category_name=category.name if category else None,
            category_confidence=analysis.category_confidence,
            category_ranking=analysis.category_ranking,
            decision=analysis.decision,
            confidence=analysis.confidence,
            matched_policy=matched,
            duplicate_of_document_id=analysis.duplicate_of_document_id if conflict and not conflict.get("restricted") else None,
            signals=analysis.signals if matched and not matched.get("restricted") else [],
            candidates=candidates,
            amendment_targets=targets,
            conflict=conflict,
            missing_fields=analysis.missing_fields,
            review_status=analysis.review_status,
            reviewed_by_id=analysis.reviewed_by_id,
            reviewed_at=analysis.reviewed_at,
            resolution=analysis.resolution,
            suggested_initial_version=self._suggested_version(analysis),
        )

    def _related_policy_id(self, analysis: DocumentAnalysis) -> uuid.UUID | str | None:
        """The policy an upload most likely belongs to when its analysis matched none, read now:
        files analysed together cannot see each other's policies, which are created later.

        * the policy its bulk-upload group was filed under (the person arranged the files as
          versions of one policy), or the policy a file of the group was confirmed into;
        * for a (near-)copy of another document, that document's policy (a reissue).
        """
        from app.modules.uploads.model import UploadBatchGroup, UploadBatchItem

        item = self.session.scalar(select(UploadBatchItem).where(UploadBatchItem.document_id == analysis.document_id))
        if item is not None:
            group = self.session.get(UploadBatchGroup, item.group_id)
            if group is not None and group.policy_id:
                return group.policy_id
            sibling_policy = self.session.scalar(
                select(Document.policy_id)
                .join(UploadBatchItem, UploadBatchItem.document_id == Document.id)
                .where(UploadBatchItem.group_id == item.group_id, Document.policy_id.is_not(None))
                .order_by(UploadBatchItem.position).limit(1)
            )
            if sibling_policy:
                return sibling_policy
        if analysis.decision in DUPLICATE_DECISIONS:
            if analysis.duplicate_of_document_id:
                existing = self.session.get(Document, analysis.duplicate_of_document_id)
                if existing is not None and existing.policy_id:
                    return existing.policy_id
            return (analysis.conflict or {}).get("existing_policy_id")
        return None

    def _policy_summary(self, policy: Policy) -> dict[str, Any]:
        versions = self.session.scalars(
            select(PolicyVersion)
            .where(PolicyVersion.policy_id == policy.id, PolicyVersion.status == VersionStatus.ACTIVE)
            .order_by(PolicyVersion.effective_from.desc())
        ).all()
        latest = versions[0] if versions else None
        return {
            "policy_id": str(policy.id),
            "name": policy.name,
            "policy_number": policy.policy_number,
            "category_id": str(policy.category_id),
            "version_count": len(versions),
            "latest_version": {
                "id": str(latest.id),
                "label": latest.version_label,
                "effective_from": latest.effective_from.isoformat(),
            } if latest else None,
        }

    @staticmethod
    def _suggested_version(analysis: DocumentAnalysis) -> dict[str, Any] | None:
        detected = analysis.detected or {}
        label = (detected.get("version_label") or {}).get("value")
        if analysis.decision in (Decision.NEW_POLICY, Decision.EXISTING_POLICY_AMENDMENT):
            label = label or "1"
        return {
            "version_label": label,
            "revision_number": (detected.get("revision_number") or {}).get("value"),
            "effective_from": detected.get("effective_date"),
            "effective_date_evidence": detected.get("effective_date_evidence"),
        }
