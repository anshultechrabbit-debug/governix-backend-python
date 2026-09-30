import uuid
from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.core.dependencies import get_app_settings
from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.core.security import decode_access_token
from app.modules.assignments.model import PolicyAssignment
from app.modules.auth.permissions import Permission, Principal, Role
from app.modules.organizations.model import Organization, OrganizationStatus
from app.modules.users.model import User

_bearer = HTTPBearer(auto_error=False)


def principal_from_user(user: User, session: Session) -> Principal:
    """The caller's access, read fresh: a User's assignments take effect on their next request."""
    role = Role(user.role)
    assigned: frozenset[uuid.UUID] = frozenset()
    if role is Role.DEPARTMENT_USER:
        assigned = frozenset(session.scalars(
            select(PolicyAssignment.policy_id).where(
                PolicyAssignment.user_id == user.id, PolicyAssignment.removed_at.is_(None)
            )
        ))
    return Principal(
        user_id=user.id,
        role=role,
        organization_id=user.organization_id,
        branch_id=user.branch_id,
        department_id=user.department_id,
        assigned_policy_ids=assigned,
    )


def get_current_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_app_settings)],
    db: Annotated[Session, Depends(get_db)],
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Authentication required.")
    claims = decode_access_token(settings, credentials.credentials)
    try:
        user_id = uuid.UUID(claims["sub"])
    except ValueError:
        raise AuthenticationError("Invalid access token.", code="INVALID_TOKEN") from None

    # Always re-read the user: deactivation, role changes and suspended
    # organisations take effect immediately, not when the token expires.
    user = db.get(User, user_id)
    if user is None or not user.is_active or user.token_version != claims.get("tv"):
        raise AuthenticationError("Session is no longer valid.", code="INVALID_TOKEN")
    if user.organization_id is not None:
        organization = db.get(Organization, user.organization_id)
        if organization is None or organization.status != OrganizationStatus.ACTIVE:
            raise AuthenticationError("Organization is not active.", code="ORGANIZATION_SUSPENDED")

    principal = principal_from_user(user, db)
    request.state.principal = principal
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def require(*permissions: Permission) -> Callable[[Principal], Principal]:
    """Dependency: the caller must hold every listed permission."""

    def dependency(principal: CurrentPrincipal) -> Principal:
        if not all(principal.has(p) for p in permissions):
            raise PermissionDeniedError("You do not have permission to perform this action.")
        return principal

    return dependency
