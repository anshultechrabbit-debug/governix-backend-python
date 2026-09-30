import uuid
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.modules.auth.acl import visible_clause
from app.modules.auth.permissions import Principal
from app.modules.policies.model import Policy, PolicyVersion, VersionStatus
from app.modules.versions.timeline import effective_on


def policy_visible(principal: Principal):
    return visible_clause(principal, Policy.organization_id, Policy.branch_id, Policy.department_id, Policy.id)


def arranged_policy_order():
    """Documents in a category as arranged; not yet arranged ones first, newest first."""
    return (Policy.display_order.asc().nulls_first(), Policy.created_at.desc(), Policy.id)


class PolicyRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_visible(self, principal: Principal, policy_id: uuid.UUID) -> Policy | None:
        return self.session.scalar(select(Policy).where(Policy.id == policy_id, policy_visible(principal)))

    def find(
        self,
        principal: Principal,
        *,
        search: str | None,
        category_id: uuid.UUID | None,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Policy], int]:
        query = select(Policy).where(policy_visible(principal))
        if category_id:
            query = query.where(Policy.category_id == category_id)
        if status:
            query = query.where(Policy.status == status)
        if search:
            term = search.strip()
            query = query.where(or_(
                Policy.name.ilike(f"%{term}%"),
                func.upper(Policy.policy_number) == term.upper(),
                func.similarity(Policy.normalized_name, term.lower()) >= 0.3,
            ))
        total = self.session.scalar(select(func.count()).select_from(query.subquery()))
        items = self.session.scalars(query.order_by(Policy.name).limit(limit).offset(offset)).all()
        return list(items), total or 0

    def current_versions(self, policy_ids: list[uuid.UUID], as_of: date) -> dict[uuid.UUID, PolicyVersion]:
        if not policy_ids:
            return {}
        rows = self.session.scalars(
            select(PolicyVersion).where(
                PolicyVersion.policy_id.in_(policy_ids),
                PolicyVersion.status == VersionStatus.ACTIVE,
                effective_on(as_of),
            )
        )
        return {v.policy_id: v for v in rows}

    def version_counts(self, policy_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not policy_ids:
            return {}
        rows = self.session.execute(
            select(PolicyVersion.policy_id, func.count())
            .where(PolicyVersion.policy_id.in_(policy_ids), PolicyVersion.status == VersionStatus.ACTIVE)
            .group_by(PolicyVersion.policy_id)
        )
        return dict(rows.all())

    def versions(self, policy_id: uuid.UUID) -> list[PolicyVersion]:
        return list(self.session.scalars(
            select(PolicyVersion).where(PolicyVersion.policy_id == policy_id).order_by(PolicyVersion.effective_from)
        ))
