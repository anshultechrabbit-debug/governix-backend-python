"""Assigning policies to Users (spec §27-28).

    org admin       any policy of the organisation to any User in it
    branch manager  a policy they can see (global or their branch) to a User of their branch

Either way the policy's scope must reach the User (a Branch A policy is never
assignable to a Branch B User), so an assignment cannot widen what a branch
shares. Removing an assignment keeps the row as history.
"""

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from app.modules.assignments.model import PolicyAssignment
from app.modules.audit.service import record_event
from app.modules.auth.acl import in_scope
from app.modules.auth.permissions import Principal, Role
from app.modules.auth.scope import tenant_id
from app.modules.categories.model import Category
from app.modules.notifications import service as notifications
from app.modules.policies.model import Policy, PolicyStatus
from app.modules.policies.repository import PolicyRepository
from app.modules.users.model import User


class AssignmentRead(BaseModel):
    id: uuid.UUID
    policy_id: uuid.UUID
    policy_name: str
    category_id: uuid.UUID
    category_name: str | None
    user_id: uuid.UUID
    user_name: str
    user_email: str
    branch_id: uuid.UUID | None
    assigned_by_name: str | None
    assigned_at: datetime
    removed_at: datetime | None
    removed_by_name: str | None


class AssignableUser(BaseModel):
    id: uuid.UUID
    full_name: str
    email: str
    branch_id: uuid.UUID | None
    department_id: uuid.UUID | None
    assigned: bool


def _as_reader(user: User) -> Principal:
    return Principal(user_id=user.id, role=Role(user.role), organization_id=user.organization_id,
                     branch_id=user.branch_id, department_id=user.department_id)


class AssignmentService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.policies = PolicyRepository(session)

    # --- checks ---------------------------------------------------------------------

    def _policy(self, principal: Principal, policy_id: uuid.UUID) -> Policy:
        policy = self.policies.get_visible(principal, policy_id)
        if policy is None:
            raise NotFoundError("Policy not found.")
        return policy

    def _manageable_users(self, principal: Principal):
        query = select(User).where(
            User.organization_id == tenant_id(principal), User.role == Role.DEPARTMENT_USER,
        )
        if principal.role is Role.BRANCH_MANAGER:
            query = query.where(User.branch_id == principal.branch_id)
        elif principal.role is not Role.ORG_ADMIN:
            raise PermissionDeniedError("You cannot assign policies.")
        return query

    def _user(self, principal: Principal, user_id: uuid.UUID) -> User:
        user = self.session.scalar(self._manageable_users(principal).where(User.id == user_id))
        if user is None:
            raise NotFoundError("User not found.")
        return user

    @staticmethod
    def _ensure_reaches(policy: Policy, user: User) -> None:
        if not in_scope(_as_reader(user), policy.organization_id, policy.branch_id, policy.department_id):
            raise ValidationError(
                f'"{policy.name}" belongs to another branch or department and cannot be assigned to {user.full_name}.',
                code="OUT_OF_SCOPE",
            )

    # --- changes ----------------------------------------------------------------------

    def assign(self, principal: Principal, policy_id: uuid.UUID, user_ids: list[uuid.UUID]) -> list[AssignmentRead]:
        policy = self._policy(principal, policy_id)
        if policy.status != PolicyStatus.ACTIVE:
            raise ValidationError("An archived policy cannot be assigned.")
        users = [self._user(principal, user_id) for user_id in dict.fromkeys(user_ids)]
        for user in users:
            self._ensure_reaches(policy, user)
        for user in users:
            self._add(principal, policy, user)
        self.session.commit()
        return self.for_policy(principal, policy_id)

    def assign_to_user(self, principal: Principal, user_id: uuid.UUID, policy_ids: list[uuid.UUID]) -> list[AssignmentRead]:
        user = self._user(principal, user_id)
        policies = [self._policy(principal, policy_id) for policy_id in dict.fromkeys(policy_ids)]
        for policy in policies:
            if policy.status != PolicyStatus.ACTIVE:
                raise ValidationError(f'"{policy.name}" is archived and cannot be assigned.')
            self._ensure_reaches(policy, user)
        for policy in policies:
            self._add(principal, policy, user)
        self.session.commit()
        return self.for_user(principal, user_id)

    def _add(self, principal: Principal, policy: Policy, user: User) -> None:
        live = self.session.scalar(select(PolicyAssignment).where(
            PolicyAssignment.policy_id == policy.id, PolicyAssignment.user_id == user.id,
            PolicyAssignment.removed_at.is_(None),
        ).with_for_update())
        if live is not None:
            return
        assignment = PolicyAssignment(
            organization_id=policy.organization_id, policy_id=policy.id, user_id=user.id,
            assigned_by_id=principal.user_id,
        )
        self.session.add(assignment)
        self.session.flush()
        record_event(
            self.session, "policy.assigned", actor=principal, resource_type="policy", resource_id=policy.id,
            details={"user_id": str(user.id), "user": user.full_name, "policy": policy.name},
        )
        notifications.assignment_changed(self.session, policy, user.id, assigned=True, actor_id=principal.user_id)

    def unassign(self, principal: Principal, policy_id: uuid.UUID, user_id: uuid.UUID) -> None:
        policy = self._policy(principal, policy_id)
        user = self._user(principal, user_id)
        live = self.session.scalar(select(PolicyAssignment).where(
            PolicyAssignment.policy_id == policy.id, PolicyAssignment.user_id == user.id,
            PolicyAssignment.removed_at.is_(None),
        ).with_for_update())
        if live is None:
            raise NotFoundError("This policy is not assigned to the user.")
        live.removed_at, live.removed_by_id = datetime.now(UTC), principal.user_id
        record_event(
            self.session, "policy.unassigned", actor=principal, resource_type="policy", resource_id=policy.id,
            details={"user_id": str(user.id), "user": user.full_name, "policy": policy.name},
        )
        notifications.assignment_changed(self.session, policy, user.id, assigned=False, actor_id=principal.user_id)
        self.session.commit()

    # --- reads ----------------------------------------------------------------------------

    def _reads(self, query) -> list[AssignmentRead]:
        assigner, remover = aliased(User), aliased(User)
        rows = self.session.execute(
            query.add_columns(Policy, User, Category.name, assigner.full_name, remover.full_name)
            .join(Policy, Policy.id == PolicyAssignment.policy_id)
            .join(User, User.id == PolicyAssignment.user_id)
            .outerjoin(Category, Category.id == Policy.category_id)
            .outerjoin(assigner, assigner.id == PolicyAssignment.assigned_by_id)
            .outerjoin(remover, remover.id == PolicyAssignment.removed_by_id)
            .order_by(PolicyAssignment.removed_at.desc().nulls_first(), PolicyAssignment.created_at.desc())
        ).all()
        return [
            AssignmentRead(
                id=a.id, policy_id=p.id, policy_name=p.name, category_id=p.category_id, category_name=category,
                user_id=u.id, user_name=u.full_name, user_email=u.email, branch_id=u.branch_id,
                assigned_by_name=by, assigned_at=a.created_at, removed_at=a.removed_at, removed_by_name=removed_by,
            )
            for a, p, u, category, by, removed_by in rows
        ]

    def for_policy(self, principal: Principal, policy_id: uuid.UUID, *, include_removed: bool = False) -> list[AssignmentRead]:
        policy = self._policy(principal, policy_id)
        users = self._manageable_users(principal).with_only_columns(User.id)
        query = select(PolicyAssignment).where(PolicyAssignment.policy_id == policy.id, PolicyAssignment.user_id.in_(users))
        if not include_removed:
            query = query.where(PolicyAssignment.removed_at.is_(None))
        return self._reads(query)

    def for_user(self, principal: Principal, user_id: uuid.UUID, *, include_removed: bool = False) -> list[AssignmentRead]:
        user = self._user(principal, user_id)
        query = select(PolicyAssignment).where(PolicyAssignment.user_id == user.id)
        if not include_removed:
            query = query.where(PolicyAssignment.removed_at.is_(None))
        return self._reads(query)

    def assignable_users(self, principal: Principal, policy_id: uuid.UUID, search: str | None) -> list[AssignableUser]:
        """Users the caller may assign this policy to (its scope reaches them), with their current state."""
        policy = self._policy(principal, policy_id)
        query = self._manageable_users(principal).where(User.is_active)
        if policy.branch_id is not None:
            query = query.where(User.branch_id == policy.branch_id)
            if policy.department_id is not None:
                query = query.where(User.department_id == policy.department_id)
        if search and search.strip():
            term = f"%{search.strip().lower()}%"
            query = query.where(func.lower(User.full_name).like(term) | func.lower(User.email).like(term))
        users = self.session.scalars(query.order_by(User.full_name).limit(200)).all()
        assigned = set(self.session.scalars(select(PolicyAssignment.user_id).where(
            PolicyAssignment.policy_id == policy.id, PolicyAssignment.removed_at.is_(None),
        )))
        return [
            AssignableUser(id=u.id, full_name=u.full_name, email=u.email, branch_id=u.branch_id,
                           department_id=u.department_id, assigned=u.id in assigned)
            for u in users
        ]
