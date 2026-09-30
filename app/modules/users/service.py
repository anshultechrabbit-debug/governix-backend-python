import uuid
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.core.database import translate_unique_violation
from app.core.exceptions import NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import hash_password
from app.modules.audit.service import record_event
from app.modules.auth.model import RefreshToken
from app.modules.auth.permissions import ASSIGNABLE_ROLES, Principal, Role
from app.modules.branches.repository import BranchRepository
from app.modules.departments.repository import DepartmentRepository
from app.modules.organizations.repository import OrganizationRepository
from app.modules.users.model import User
from app.modules.users.repository import UserRepository
from app.modules.users.schema import UserCreate, UserUpdate

EMAIL_TAKEN = "A user with this email already exists."


class UserService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = UserRepository(session)
        self.organizations = OrganizationRepository(session)
        self.branches = BranchRepository(session)
        self.departments = DepartmentRepository(session)

    # --- scope rules -------------------------------------------------------

    def _can_manage(self, principal: Principal, target: User) -> bool:
        if Role(target.role) not in ASSIGNABLE_ROLES[principal.role]:
            return False
        if principal.role is Role.MASTER_ADMIN:
            return True
        if target.organization_id != principal.organization_id:
            return False
        if principal.role is Role.BRANCH_MANAGER:
            return target.branch_id == principal.branch_id
        return True

    def _resolve_scope(
        self,
        principal: Principal,
        role: Role,
        organization_id: uuid.UUID | None,
        branch_id: uuid.UUID | None,
        department_id: uuid.UUID | None,
    ) -> tuple[uuid.UUID | None, uuid.UUID | None, uuid.UUID | None]:
        """Validate and normalise the org/branch/department a user with `role` belongs to."""
        if role not in ASSIGNABLE_ROLES[principal.role]:
            raise PermissionDeniedError(f"You cannot assign the role {role}.")

        if principal.role is Role.MASTER_ADMIN:
            if organization_id is None or self.organizations.get(organization_id) is None:
                raise ValidationError("A valid organization_id is required.")
        else:
            organization_id = principal.organization_id
        if principal.role is Role.BRANCH_MANAGER:
            branch_id = principal.branch_id

        if role in (Role.ORG_ADMIN,):
            return organization_id, None, None

        if branch_id is None or self.branches.get_in_org(organization_id, branch_id) is None:
            raise ValidationError("A valid branch_id in this organization is required.")
        if role is Role.BRANCH_MANAGER:
            return organization_id, branch_id, None

        # A User belongs to the branch; a department is optional.
        if department_id is None:
            return organization_id, branch_id, None
        department = self.departments.get_in_org(organization_id, department_id)
        if department is None or department.branch_id != branch_id:
            raise ValidationError("The department does not belong to the selected branch.")
        return organization_id, branch_id, department_id

    # --- operations --------------------------------------------------------

    def create(self, principal: Principal, data: UserCreate) -> User:
        organization_id, branch_id, department_id = self._resolve_scope(
            principal, data.role, data.organization_id, data.branch_id, data.department_id
        )
        if self.repository.email_exists(data.email):
            raise ValidationError(EMAIL_TAKEN, code="EMAIL_TAKEN")
        user = User(
            email=data.email.lower(),
            full_name=data.full_name,
            password_hash=hash_password(data.password),
            role=data.role,
            organization_id=organization_id,
            branch_id=branch_id,
            department_id=department_id,
        )
        with translate_unique_violation(self.session, EMAIL_TAKEN, code="EMAIL_TAKEN"):
            self.session.add(user)
            self.session.flush()
            record_event(
                self.session,
                "user.created",
                actor=principal,
                organization_id=organization_id,
                resource_type="user",
                resource_id=user.id,
                details={"email": user.email, "role": data.role},
            )
            self.session.commit()
        return user

    def get(self, principal: Principal, user_id: uuid.UUID) -> User:
        user = self.repository.get(user_id)
        if user is None or (user.id != principal.user_id and not self._can_manage(principal, user)):
            raise NotFoundError("User not found.")
        return user

    def list(
        self, principal: Principal, *, search: str | None, limit: int, offset: int
    ) -> tuple[list[User], int]:
        if principal.role is Role.MASTER_ADMIN:
            return self.repository.list(
                organization_id=None, branch_id=None,
                roles=frozenset({Role.ORG_ADMIN}), search=search, limit=limit, offset=offset,
            )
        return self.repository.list(
            organization_id=principal.organization_id,
            branch_id=principal.branch_id if principal.role is Role.BRANCH_MANAGER else None,
            roles=frozenset(ASSIGNABLE_ROLES[principal.role]) if principal.role is Role.BRANCH_MANAGER else None,
            search=search,
            limit=limit,
            offset=offset,
        )

    def update(self, principal: Principal, user_id: uuid.UUID, data: UserUpdate) -> User:
        user = self.repository.get(user_id)
        if user is None or not self._can_manage(principal, user):
            raise NotFoundError("User not found.")
        changes = data.model_dump(exclude_unset=True)
        if user.id == principal.user_id and (
            changes.get("is_active") is False or "role" in changes
        ):
            raise ValidationError("You cannot deactivate yourself or change your own role.")

        scope_fields = {"role", "branch_id", "department_id"}
        if scope_fields & changes.keys():
            role = Role(changes.get("role") or user.role)
            organization_id, branch_id, department_id = self._resolve_scope(
                principal,
                role,
                user.organization_id,
                changes.get("branch_id", user.branch_id),
                changes.get("department_id", user.department_id),
            )
            user.role, user.branch_id, user.department_id = role, branch_id, department_id
            user.token_version += 1
        if changes.get("full_name"):
            user.full_name = changes["full_name"]
        if changes.get("is_active") is not None and changes["is_active"] != user.is_active:
            user.is_active = changes["is_active"]
            user.token_version += 1
            if not user.is_active:
                self._revoke_refresh_tokens(user.id)

        record_event(
            self.session, "user.updated", actor=principal, organization_id=user.organization_id,
            resource_type="user", resource_id=user.id,
            details={"changes": {k: str(v) for k, v in changes.items()}},
        )
        self.session.commit()
        return user

    def reset_password(self, principal: Principal, user_id: uuid.UUID, new_password: str) -> None:
        user = self.repository.get(user_id)
        if user is None or not self._can_manage(principal, user):
            raise NotFoundError("User not found.")
        user.password_hash = hash_password(new_password)
        user.token_version += 1
        user.failed_login_attempts = 0
        user.locked_until = None
        self._revoke_refresh_tokens(user.id)
        record_event(
            self.session, "user.password_reset", actor=principal,
            organization_id=user.organization_id, resource_type="user", resource_id=user.id,
        )
        self.session.commit()

    def _revoke_refresh_tokens(self, user_id: uuid.UUID) -> None:
        self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )


def create_master_admin(session: Session, email: str, full_name: str, password: str) -> User:
    """Bootstrap the first platform administrator (CLI only; no API can create one)."""
    repository = UserRepository(session)
    if repository.email_exists(email):
        raise ValidationError(EMAIL_TAKEN, code="EMAIL_TAKEN")
    user = User(
        email=email.lower(),
        full_name=full_name,
        password_hash=hash_password(password),
        role=Role.MASTER_ADMIN,
    )
    session.add(user)
    session.flush()
    record_event(session, "user.master_admin_bootstrapped", resource_type="user", resource_id=user.id)
    session.commit()
    return user
