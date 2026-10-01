import logging
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from app.core.database import translate_unique_violation
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.modules.audit.service import record_event
from app.modules.auth.acl import can_see, ensure_can_write_scope
from app.modules.auth.permissions import Principal, Role
from app.modules.auth.scope import tenant_id
from app.modules.branches.repository import BranchRepository
from app.modules.categories.model import Category
from app.modules.categories.repository import CategoryRepository
from app.modules.departments.repository import DepartmentRepository
from app.infrastructure.storage.base import Storage
from app.modules.documents.model import Document, DocumentStatus
from app.modules.documents.repository import DocumentRepository
from app.modules.documents.scope_sync import sync_document_scope
from app.modules.ingestion.analysis.metadata import normalize_title
from app.modules.notifications import service as notifications
from app.modules.organizations.repository import bump_knowledge_version
from app.modules.policies.model import (
    DocumentRelationship,
    Policy,
    PolicyStatus,
    PolicyVersion,
    RelationStatus,
    RelationType,
    VersionStatus,
)
from app.modules.policies.repository import PolicyRepository, arranged_policy_order
from app.modules.policies.schema import (
    PolicyCreate,
    PolicyDetail,
    PolicyRead,
    PolicyUpdate,
    RelationshipCreate,
    RelationshipRead,
    VersionRead,
)
from app.modules.search.model import Chunk
from app.modules.versions.timeline import (
    active_versions,
    compare_versions,
    in_force,
    plan_order,
    refresh_change_summaries,
    reorder_versions,
    restore_version,
    timeline_state,
    version_as_of,
    withdraw_version,
)


def today() -> date:
    return datetime.now(UTC).date()


def version_read(version: PolicyVersion, as_of: date) -> VersionRead:
    read = VersionRead.model_validate(version)
    read.timeline_state = timeline_state(version, as_of)
    read.has_ai_summary = version.ai_summary is not None
    return read


logger = logging.getLogger(__name__)
# The pipeline is still writing these; deleting under it would leave it half-done.
_PROCESSING = (DocumentStatus.UPLOADED, DocumentStatus.PROCESSING, DocumentStatus.INDEXING)


class PolicyService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = PolicyRepository(session)
        self.documents = DocumentRepository(session)
        self.categories = CategoryRepository(session)

    def get(self, principal: Principal, policy_id: uuid.UUID) -> Policy:
        policy = self.repository.get_visible(principal, policy_id)
        if policy is None:
            raise NotFoundError("Policy not found.")
        return policy

    def list_policies(self, principal: Principal, *, as_of: date | None, **filters) -> tuple[list[PolicyRead], int]:
        as_of = as_of or today()
        policies, total = self.repository.find(principal, **filters)
        ids = [p.id for p in policies]
        current = self.repository.current_versions(ids, as_of)
        counts = self.repository.version_counts(ids)
        items = []
        for policy in policies:
            read = PolicyRead.model_validate(policy)
            if version := current.get(policy.id):
                read.current_version = version_read(version, as_of)
            read.version_count = counts.get(policy.id, 0)
            items.append(read)
        return items, total

    def detail(self, principal: Principal, policy_id: uuid.UUID) -> PolicyDetail:
        policy = self.get(principal, policy_id)
        as_of = today()
        versions = self.repository.versions(policy.id)
        reads = [version_read(v, as_of) for v in versions]
        current = next((r for r in reads if r.timeline_state == "current"), None)
        detail = PolicyDetail(
            **PolicyRead.model_validate(policy).model_dump(),
            versions=reads,
            incoming_relationships=self._incoming(principal, policy),
            outgoing_relationships=self._outgoing(principal, policy),
        )
        detail.current_version = current
        detail.version_count = sum(1 for v in versions if v.status == VersionStatus.ACTIVE)
        return detail

    def version(self, principal: Principal, policy_id: uuid.UUID, version_id: uuid.UUID) -> PolicyVersion:
        policy = self.get(principal, policy_id)
        version = self.session.get(PolicyVersion, version_id)
        if version is None or version.policy_id != policy.id:
            raise NotFoundError("Version not found.")
        return version

    def compare(self, principal: Principal, policy_id: uuid.UUID, base_id: uuid.UUID, target_id: uuid.UUID) -> dict:
        base = self.version(principal, policy_id, base_id)
        target = self.version(principal, policy_id, target_id)
        return compare_versions(self.session, base, target)

    def update(self, principal: Principal, policy_id: uuid.UUID, data: PolicyUpdate) -> Policy:
        policy = self.get(principal, policy_id)
        ensure_can_write_scope(principal, policy.branch_id, policy.department_id)
        changes = data.model_dump(exclude_unset=True, exclude_none=True)
        if "category_id" in changes and self.categories.get_in_org(policy.organization_id, changes["category_id"]) is None:
            raise ValidationError("Category not found.")
        if changes.get("category_id", policy.category_id) != policy.category_id:
            policy.display_order = None  # arranged positions belong to the old category
        if "name" in changes:
            changes["name"] = " ".join(changes["name"].split())
            policy.normalized_name = normalize_title(changes["name"])
        for field, value in changes.items():
            setattr(policy, field, value)
        if "status" in changes or "category_id" in changes or "name" in changes:
            bump_knowledge_version(self.session, policy.organization_id)
        record_event(
            self.session, "policy.updated", actor=principal, resource_type="policy", resource_id=policy.id,
            details={"changes": {k: str(v) for k, v in changes.items()}},
        )
        self.session.commit()
        return policy

    def withdraw(self, principal: Principal, policy_id: uuid.UUID, version_id: uuid.UUID, reason: str) -> PolicyVersion:
        version = self.version(principal, policy_id, version_id)
        policy = self.session.get(Policy, policy_id)
        ensure_can_write_scope(principal, policy.branch_id, policy.department_id)
        if version.status != VersionStatus.ACTIVE:
            raise ValidationError("Only active versions can be withdrawn.")
        neighbours = (version.supersedes_version_id, version.superseded_by_version_id)
        before = self._in_force(policy.id)
        withdraw_version(self.session, version, reason)
        following = self.session.get(PolicyVersion, neighbours[1]) if neighbours[1] else None
        refresh_change_summaries(self.session, following)
        bump_knowledge_version(self.session, policy.organization_id)
        record_event(
            self.session, "policy_version.withdrawn", actor=principal, resource_type="policy_version",
            resource_id=version.id, details={"policy_id": str(policy_id), "reason": reason},
        )
        self._announce_if_latest_changed(principal, policy, before, f"Version {version.version_label} was withdrawn: {reason}")
        self.session.commit()
        return version

    def restore(self, principal: Principal, policy_id: uuid.UUID, version_id: uuid.UUID) -> PolicyVersion:
        """Activate an archived (withdrawn) version: it rejoins the timeline at its effective date."""
        version = self.version(principal, policy_id, version_id)
        policy = self.session.get(Policy, policy_id)
        ensure_can_write_scope(principal, policy.branch_id, policy.department_id)
        if version.status != VersionStatus.WITHDRAWN:
            raise ValidationError("Only withdrawn versions can be restored.")
        before = self._in_force(policy.id)
        _, following = restore_version(self.session, version)
        refresh_change_summaries(self.session, version, following)
        bump_knowledge_version(self.session, policy.organization_id)
        record_event(
            self.session, "policy_version.restored", actor=principal, resource_type="policy_version",
            resource_id=version.id, details={"policy_id": str(policy_id), "label": version.version_label},
        )
        self._announce_if_latest_changed(principal, policy, before, f"Version {version.version_label} was restored.")
        self.session.commit()
        return version

    # --- creating a policy by hand (spec §11) --------------------------------------

    def create(self, principal: Principal, data: PolicyCreate) -> Policy:
        organization_id = tenant_id(principal)
        category = self.categories.get_in_org(organization_id, data.category_id)
        if category is None or not category.is_active:
            raise ValidationError("Category not found.")
        branch_id, department_id = None, None
        if data.scope == "global":
            if principal.role is not Role.ORG_ADMIN:
                raise PermissionDeniedError("Only organisation admins create global policies.")
        else:
            branch_id = data.branch_id or (principal.branch_id if principal.role is not Role.ORG_ADMIN else None)
            if branch_id is None:
                raise ValidationError("Choose the branch for a branch policy.")
            if BranchRepository(self.session).get_in_org(organization_id, branch_id) is None:
                raise ValidationError("Branch not found.")
            if data.department_id is not None:
                department = DepartmentRepository(self.session).get_in_org(organization_id, data.department_id)
                if department is None or department.branch_id != branch_id:
                    raise ValidationError("Department not found in the selected branch.")
                department_id = department.id
        ensure_can_write_scope(principal, branch_id, department_id)
        name = " ".join(data.name.split())
        if not normalize_title(name):
            raise ValidationError("Policy name is required.")
        policy = Policy(
            organization_id=organization_id, branch_id=branch_id, department_id=department_id,
            category_id=category.id, name=name[:500], normalized_name=normalize_title(name)[:500],
            policy_number=(data.policy_number or "").strip().upper() or None,
            description=(data.description or "").strip() or None, owner=data.owner, created_by_id=principal.user_id,
        )
        with translate_unique_violation(self.session, "A policy with this policy number already exists.", "POLICY_NUMBER_EXISTS"):
            self.session.add(policy)
            self.session.flush()
            record_event(
                self.session, "policy.created", actor=principal, resource_type="policy", resource_id=policy.id,
                details={"name": policy.name, "category": category.name, "scope": data.scope, "manual": True},
            )
            self.session.commit()
        return policy

    # --- version order and moves (spec §18-19) ----------------------------------------

    def _in_force(self, policy_id: uuid.UUID) -> tuple[uuid.UUID, str] | None:
        """(id, label) of the version in force, captured before a change renumbers anything."""
        version = version_as_of(self.session, policy_id, today())
        return (version.id, version.version_label) if version else None

    def _announce_if_latest_changed(self, principal: Principal, policy: Policy, before: tuple[uuid.UUID, str] | None, reason: str) -> None:
        after = version_as_of(self.session, policy.id, today())
        if (before[0] if before else None) == (after.id if after else None):
            return
        record_event(
            self.session, "policy.latest_version_changed", actor=principal, resource_type="policy", resource_id=policy.id,
            details={"old_version_id": str(before[0]) if before else None, "old_version": before[1] if before else None,
                     "new_version_id": str(after.id) if after else None,
                     "new_version": after.version_label if after else None, "reason": reason},
        )
        notifications.latest_changed(self.session, policy, after, reason=reason, actor_id=principal.user_id)

    def reorder(self, principal: Principal, policy_id: uuid.UUID, version_ids: list[uuid.UUID], *, confirm_latest_change: bool) -> Policy:
        """Make the given order (newest first) the timeline. The first version becomes the latest."""
        policy = self.get(principal, policy_id)
        ensure_can_write_scope(principal, policy.branch_id, policy.department_id)
        versions = {v.id: v for v in active_versions(self.session, policy.id, lock=True)}
        if len(version_ids) != len(set(version_ids)) or set(version_ids) != set(versions):
            raise ValidationError("List every active version of the policy exactly once, newest first.")
        newest_first = [versions[v] for v in version_ids]
        current_order = sorted(versions.values(), key=lambda v: v.effective_from, reverse=True)
        if [v.id for v in current_order] == version_ids:
            return policy
        before = self._in_force(policy.id)
        planned = plan_order(newest_first)  # raises on a conflict with a real date
        end = current_order[0].effective_to
        after_id = in_force(planned, newest_first, end, today())
        if (before[0] if before else None) != after_id and not confirm_latest_change:
            after = versions.get(after_id) if after_id else None
            raise ConflictError(
                "This order changes which version is in force. Confirm to make it the latest version.",
                code="LATEST_CHANGE_REQUIRES_CONFIRMATION",
                details={
                    "current": {"id": str(before[0]), "label": before[1]} if before else None,
                    "new": {"id": str(after.id), "label": after.version_label} if after else None,
                },
            )
        old_labels = [v.version_label for v in newest_first]
        reorder_versions(self.session, policy, newest_first)
        bump_knowledge_version(self.session, policy.organization_id)
        record_event(
            self.session, "policy.versions_reordered", actor=principal, resource_type="policy", resource_id=policy.id,
            details={"order_before": [v.version_label for v in current_order], "order_requested": old_labels,
                     "order_after": [v.version_label for v in newest_first]},
        )
        latest = newest_first[0]
        self._announce_if_latest_changed(
            principal, policy, before, f"Version order changed; the latest version is now {latest.version_label}.",
        )
        self.session.commit()
        return policy

    def move_version(self, principal: Principal, policy_id: uuid.UUID, version_id: uuid.UUID,
                     target_policy_id: uuid.UUID, *, confirm: bool) -> PolicyVersion:
        """Move a document (one version) to another policy's version group, keeping its history."""
        source = self.get(principal, policy_id)
        target = self.get(principal, target_policy_id)
        if target.id == source.id:
            raise ValidationError("Choose a different policy.")
        ensure_can_write_scope(principal, source.branch_id, source.department_id)
        ensure_can_write_scope(principal, target.branch_id, target.department_id)
        if target.status != PolicyStatus.ACTIVE:
            raise ConflictError("The target policy is archived.", code="INVALID_STATE")
        version = self.version(principal, policy_id, version_id)
        if not confirm:
            raise ConflictError(
                f'Moving version {version.version_label} from "{source.name}" to "{target.name}" changes which '
                "policy it belongs to. Confirm to continue.",
                code="MOVE_REQUIRES_CONFIRMATION",
                details={"from_policy": str(source.id), "to_policy": str(target.id)},
            )
        # Lock both timelines in a fixed order so two moves cannot deadlock.
        for pid in sorted([source.id, target.id], key=str):
            self.session.scalar(select(Policy.id).where(Policy.id == pid).with_for_update())
        source_before, target_before = self._in_force(source.id), self._in_force(target.id)
        was_active = version.status == VersionStatus.ACTIVE
        old_label = version.version_label
        if was_active:
            following_id = version.superseded_by_version_id
            withdraw_version(self.session, version, f"Moved to {target.name}")
            refresh_change_summaries(self.session, self.session.get(PolicyVersion, following_id) if following_id else None)

        version.policy_id = target.id
        version.version_number = (self.session.scalar(
            select(func.max(PolicyVersion.version_number)).where(PolicyVersion.policy_id == target.id)
        ) or 0) + 1
        if version.version_label_auto:
            version.version_label = str(version.version_number)
        clash = self.session.scalar(select(func.max(PolicyVersion.revision_number)).where(
            PolicyVersion.policy_id == target.id, PolicyVersion.id != version.id,
            PolicyVersion.version_label == version.version_label,
        ))
        if clash is not None:
            version.revision_number = clash + 1  # the same label already exists there: keep both, as a revision
        self.session.flush()
        if was_active:
            _, following = restore_version(self.session, version)  # joins the target timeline at its date
            refresh_change_summaries(self.session, version, following)

        document = self.session.get(Document, version.document_id)
        document.policy_id, document.category_id = target.id, target.category_id
        document.branch_id, document.department_id = target.branch_id, target.department_id
        document.title = target.name
        sync_document_scope(self.session, document)
        self.session.execute(update(Chunk).where(Chunk.document_id == document.id).values(policy_id=target.id))
        self.session.execute(
            update(DocumentRelationship).where(DocumentRelationship.target_version_id == version.id)
            .values(target_policy_id=target.id)
        )
        bump_knowledge_version(self.session, source.organization_id)
        record_event(
            self.session, "document.moved", actor=principal, resource_type="document", resource_id=document.id,
            details={"from_policy_id": str(source.id), "from_policy": source.name, "to_policy_id": str(target.id),
                     "to_policy": target.name, "version_id": str(version.id), "old_label": old_label,
                     "new_label": version.version_label},
        )
        self._announce_if_latest_changed(principal, source, source_before, f"Version {old_label} was moved to {target.name}.")
        self._announce_if_latest_changed(principal, target, target_before, f"Version {version.version_label} was moved here from {source.name}.")
        self.session.commit()
        return version

    def delete(self, principal: Principal, policy_id: uuid.UUID, storage: Storage) -> dict:
        """Delete a policy for good: every version, its documents and their files.

        Chunks, pages and analyses go with the documents (database cascades);
        assignments and relationships to the policy go with it. The audit log keeps
        a record of what was deleted.
        """
        policy = self.get(principal, policy_id)
        ensure_can_write_scope(principal, policy.branch_id, policy.department_id)
        documents = list(self.session.scalars(select(Document).where(Document.policy_id == policy.id)))
        if any(d.status in _PROCESSING for d in documents):
            raise ConflictError(
                "A document of this policy is still being processed. Delete the policy once processing finishes.",
                code="INVALID_STATE",
            )
        versions = list(self.session.scalars(select(PolicyVersion).where(PolicyVersion.policy_id == policy.id)))
        version_ids = {v.id for v in versions}
        documents += [d for d in self.session.scalars(
            select(Document).where(Document.id.in_({v.document_id for v in versions}))
        ) if d not in documents]
        keys = [d.storage_key for d in documents]
        details = {
            "name": policy.name, "policy_number": policy.policy_number,
            "versions": [v.version_label for v in versions],
            "documents": [d.original_filename for d in documents],
        }

        for document in documents:
            document.policy_version_id = None
        # Versions outside this policy that point at its versions (none expected) are unlinked by the FKs.
        self.session.execute(
            update(PolicyVersion).where(PolicyVersion.id.in_(version_ids))
            .values(supersedes_version_id=None, superseded_by_version_id=None)
        )
        self.session.flush()
        self.session.execute(delete(PolicyVersion).where(PolicyVersion.id.in_(version_ids)))
        ids = {"ids": [str(d.id) for d in documents], "policy": str(policy.id)}
        self.session.execute(text(
            "DELETE FROM queue_jobs WHERE status IN ('queued', 'failed') AND payload->>'document_id' = ANY(:ids)"
        ), ids)
        self.session.execute(text(
            "DELETE FROM notifications WHERE data->>'document_id' = ANY(:ids) OR data->>'policy_id' = :policy "
            "OR link LIKE '/policies/' || :policy || '%'"
        ), ids)
        for document in documents:
            self.session.delete(document)
        self.session.flush()
        self.session.execute(delete(Policy).where(Policy.id == policy.id))
        bump_knowledge_version(self.session, policy.organization_id)  # cached answers may cite it
        record_event(
            self.session, "policy.deleted", actor=principal, resource_type="policy", resource_id=policy_id,
            details=details,
        )
        self.session.commit()
        for key in keys:
            try:
                storage.delete(key)
            except Exception:  # the records are gone either way; an orphaned file is only wasted space
                logger.warning("Could not delete the stored file %s", key, exc_info=True)
        return {"deleted": True, "versions": len(versions), "documents": len(documents)}

    def move(self, principal: Principal, policy_id: uuid.UUID, *, after_id: uuid.UUID | None, before_id: uuid.UUID | None) -> Policy:
        """Place a document after/before another in its category (neither: first) and keep that order."""
        policy = self.get(principal, policy_id)
        ensure_can_write_scope(principal, policy.branch_id, policy.department_id)
        # One arrangement at a time per category.
        self.session.scalar(select(Category.id).where(Category.id == policy.category_id).with_for_update())
        # Positions cover every document in the category, including ones this
        # person cannot see, so their relative order is kept too.
        rows = self.session.execute(
            select(Policy.id, Policy.display_order)
            .where(Policy.organization_id == policy.organization_id, Policy.category_id == policy.category_id)
            .order_by(*arranged_policy_order())
        ).all()
        current = {row.id: row.display_order for row in rows}
        ordered = [row.id for row in rows if row.id != policy.id]
        anchor = after_id or before_id
        if anchor is not None:
            if anchor == policy.id or anchor not in current:
                raise ValidationError("The other document is not in this category.")
            index = ordered.index(anchor) + (1 if after_id else 0)
        else:
            index = 0
        ordered.insert(index, policy.id)
        changed = [{"id": pid, "display_order": position} for position, pid in enumerate(ordered) if current[pid] != position]
        if changed:
            self.session.execute(update(Policy), changed)
        record_event(
            self.session, "policy.moved", actor=principal, resource_type="policy", resource_id=policy.id,
            details={"category_id": str(policy.category_id), "position": index},
        )
        self.session.commit()
        self.session.refresh(policy)
        return policy

    def add_relationship(self, principal: Principal, document_id: uuid.UUID, data: RelationshipCreate) -> DocumentRelationship:
        document = self.documents.get_visible(principal, document_id)
        if document is None:
            raise NotFoundError("Document not found.")
        ensure_can_write_scope(principal, document.branch_id, document.department_id)
        target = self.get(principal, data.target_policy_id)
        try:
            relation = RelationType(data.relation_type.upper())
        except ValueError:
            raise ValidationError(f"Unknown relation type {data.relation_type}.") from None
        if data.target_version_id:
            version = self.session.get(PolicyVersion, data.target_version_id)
            if version is None or version.policy_id != target.id:
                raise ValidationError("Target version does not belong to the related policy.")
        relationship = DocumentRelationship(
            organization_id=document.organization_id, source_document_id=document.id,
            relation_type=relation, target_policy_id=target.id, target_version_id=data.target_version_id,
            clauses=data.clauses, evidence=data.evidence, status=RelationStatus.CONFIRMED,
            created_by_id=principal.user_id,
        )
        self.session.add(relationship)
        self.session.flush()
        bump_knowledge_version(self.session, document.organization_id)
        record_event(
            self.session, "relationship.created", actor=principal, resource_type="relationship",
            resource_id=relationship.id,
            details={"type": relation, "source_document_id": str(document.id), "target_policy_id": str(target.id)},
        )
        self.session.commit()
        return relationship

    def _incoming(self, principal: Principal, policy: Policy) -> list[RelationshipRead]:
        rows = self.session.execute(
            select(DocumentRelationship, Document)
            .join(Document, Document.id == DocumentRelationship.source_document_id)
            .where(
                DocumentRelationship.target_policy_id == policy.id,
                DocumentRelationship.status == RelationStatus.CONFIRMED,
            )
            .order_by(DocumentRelationship.created_at)
        ).all()
        result = []
        for relationship, source in rows:
            if not can_see(principal, source.organization_id, source.branch_id, source.department_id, source.policy_id):
                continue
            read = RelationshipRead.model_validate(relationship)
            read.source_title, read.source_policy_id = source.title, source.policy_id
            read.target_policy_name = policy.name
            result.append(read)
        return result

    def _outgoing(self, principal: Principal, policy: Policy) -> list[RelationshipRead]:
        rows = self.session.execute(
            select(DocumentRelationship, Policy, Document)
            .join(Document, Document.id == DocumentRelationship.source_document_id)
            .join(Policy, Policy.id == DocumentRelationship.target_policy_id)
            .where(Document.policy_id == policy.id, DocumentRelationship.status == RelationStatus.CONFIRMED)
            .order_by(DocumentRelationship.created_at)
        ).all()
        result = []
        for relationship, target, source in rows:
            if not can_see(principal, target.organization_id, target.branch_id, target.department_id, target.id):
                continue
            read = RelationshipRead.model_validate(relationship)
            read.source_title, read.source_policy_id = source.title, source.policy_id
            read.target_policy_name = target.name
            result.append(read)
        return result
