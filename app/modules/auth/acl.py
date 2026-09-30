"""Data-level access control, applied inside SQL before anything is retrieved.

Every tenant resource (document, policy, section, chunk) carries three scope
columns: organization_id, branch_id (NULL = organisation-wide, a "global"
policy) and department_id (NULL = whole branch), plus the policy it belongs to.
Visibility:

    org_admin        everything in the organisation
    branch_manager   organisation-wide + everything in their branch
    department_user  (a "User") only policies assigned to them, and only while the
                     policy's scope also reaches them: organisation-wide, or their
                     branch (and department, if it has one)

A resource not (yet) part of a policy, such as a file still being processed,
is therefore never visible to a User.

`visible_clause` is the ONLY way queries should filter tenant data, so search,
RAG context and citations all share one rule. The policy column is required so
no caller can forget the assignment rule.
"""

import uuid

from sqlalchemy import ColumnElement, and_, false, or_

from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.modules.auth.permissions import Principal, Role


def visible_clause(
    principal: Principal,
    organization_col,
    branch_col,
    department_col,
    policy_col,
) -> ColumnElement[bool]:
    if principal.organization_id is None:
        return false()
    same_org = organization_col == principal.organization_id
    if principal.role is Role.ORG_ADMIN:
        return same_org
    if principal.role is Role.BRANCH_MANAGER:
        return and_(same_org, or_(branch_col.is_(None), branch_col == principal.branch_id))
    if not principal.assigned_policy_ids:
        return false()
    return and_(
        same_org,
        policy_col.in_(principal.assigned_policy_ids),
        or_(
            branch_col.is_(None),
            and_(
                branch_col == principal.branch_id,
                or_(department_col.is_(None), department_col == principal.department_id),
            ),
        ),
    )


def in_scope(
    principal: Principal,
    organization_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    department_id: uuid.UUID | None,
) -> bool:
    """Whether the branch/department scope reaches the principal, ignoring assignments.

    Used to decide what may be assigned to a User; never on its own to show data.
    """
    if principal.organization_id is None or organization_id != principal.organization_id:
        return False
    if principal.role is Role.ORG_ADMIN or branch_id is None:
        return True
    if branch_id != principal.branch_id:
        return False
    if principal.role is Role.BRANCH_MANAGER:
        return True
    return department_id is None or department_id == principal.department_id


def can_see(
    principal: Principal,
    organization_id: uuid.UUID,
    branch_id: uuid.UUID | None,
    department_id: uuid.UUID | None,
    policy_id: uuid.UUID | None,
) -> bool:
    """Python mirror of `visible_clause` for already-loaded rows."""
    if not in_scope(principal, organization_id, branch_id, department_id):
        return False
    if principal.role is Role.DEPARTMENT_USER:
        return policy_id is not None and policy_id in principal.assigned_policy_ids
    return True


def ensure_visible(principal: Principal, resource, policy_id: uuid.UUID | None, message: str = "Not found.") -> None:
    """Raise 404 (not 403) for invisible resources so their existence is not revealed."""
    if resource is None or not can_see(
        principal, resource.organization_id, resource.branch_id, resource.department_id, policy_id
    ):
        raise NotFoundError(message)


def can_write_scope(
    principal: Principal, branch_id: uuid.UUID | None, department_id: uuid.UUID | None
) -> bool:
    """Whether the principal may place or manage content at this scope.

    Callers must separately verify the branch/department belong to the organisation.
    """
    if principal.role is Role.ORG_ADMIN:
        return True
    if principal.role is Role.BRANCH_MANAGER:
        return branch_id is not None and branch_id == principal.branch_id
    if principal.role is Role.DEPARTMENT_USER:
        return branch_id == principal.branch_id and department_id == principal.department_id
    return False


def ensure_can_write_scope(
    principal: Principal, branch_id: uuid.UUID | None, department_id: uuid.UUID | None
) -> None:
    if not can_write_scope(principal, branch_id, department_id):
        raise PermissionDeniedError("You cannot manage content at this branch/department scope.")


def default_write_scope(principal: Principal) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """The narrowest sensible scope for new content when the caller specifies none."""
    if principal.role is Role.ORG_ADMIN:
        return None, None
    if principal.role is Role.BRANCH_MANAGER:
        return principal.branch_id, None
    return principal.branch_id, principal.department_id
