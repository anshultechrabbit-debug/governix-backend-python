import hashlib
import uuid
from dataclasses import dataclass
from enum import StrEnum


class Role(StrEnum):
    MASTER_ADMIN = "master_admin"
    ORG_ADMIN = "org_admin"
    BRANCH_MANAGER = "branch_manager"
    DEPARTMENT_USER = "department_user"


class Permission(StrEnum):
    ORGANIZATIONS_MANAGE = "organizations:manage"
    SYSTEM_MONITOR = "system:monitor"
    BRANCHES_MANAGE = "branches:manage"
    DEPARTMENTS_MANAGE = "departments:manage"
    USERS_MANAGE = "users:manage"
    CATEGORIES_MANAGE = "categories:manage"
    DOCUMENTS_READ = "documents:read"
    DOCUMENTS_UPLOAD = "documents:upload"
    DOCUMENTS_MANAGE = "documents:manage"
    POLICIES_READ = "policies:read"
    POLICIES_MANAGE = "policies:manage"
    POLICIES_ASSIGN = "policies:assign"  # decide which Users may read a policy
    SEARCH = "search"
    AI_QUERY = "ai:query"
    AUDIT_READ = "audit:read"
    SUPPORT = "support"  # raise and follow tickets; who handles them depends on the role


_TENANT_BASE = {
    Permission.DOCUMENTS_READ,
    Permission.POLICIES_READ,
    Permission.SEARCH,
    Permission.AI_QUERY,
    Permission.SUPPORT,
}

# The master admin runs the platform; it deliberately has no access to any
# organisation's documents or knowledge base (USERS_MANAGE is limited to
# organization admins by ASSIGNABLE_ROLES).
ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.MASTER_ADMIN: frozenset(
        {Permission.ORGANIZATIONS_MANAGE, Permission.SYSTEM_MONITOR, Permission.USERS_MANAGE, Permission.SUPPORT}
    ),
    Role.ORG_ADMIN: frozenset(
        _TENANT_BASE
        | {
            Permission.DOCUMENTS_UPLOAD,
            Permission.POLICIES_ASSIGN,
            Permission.BRANCHES_MANAGE,
            Permission.DEPARTMENTS_MANAGE,
            Permission.USERS_MANAGE,
            Permission.CATEGORIES_MANAGE,
            Permission.DOCUMENTS_MANAGE,
            Permission.POLICIES_MANAGE,
            Permission.AUDIT_READ,
        }
    ),
    Role.BRANCH_MANAGER: frozenset(
        _TENANT_BASE
        | {
            Permission.DOCUMENTS_UPLOAD,
            Permission.POLICIES_ASSIGN,
            Permission.DEPARTMENTS_MANAGE,
            Permission.USERS_MANAGE,
            Permission.DOCUMENTS_MANAGE,
            Permission.POLICIES_MANAGE,
        }
    ),
    # Users read what is assigned to them and do not upload (spec access matrix).
    Role.DEPARTMENT_USER: frozenset(_TENANT_BASE),
}

# Which roles each role may create or assign.
ASSIGNABLE_ROLES: dict[Role, frozenset[Role]] = {
    Role.MASTER_ADMIN: frozenset({Role.ORG_ADMIN}),
    Role.ORG_ADMIN: frozenset({Role.ORG_ADMIN, Role.BRANCH_MANAGER, Role.DEPARTMENT_USER}),
    Role.BRANCH_MANAGER: frozenset({Role.DEPARTMENT_USER}),
    Role.DEPARTMENT_USER: frozenset(),
}


@dataclass(frozen=True)
class Principal:
    """The authenticated caller, always built from the database, never from token claims."""

    user_id: uuid.UUID
    role: Role
    organization_id: uuid.UUID | None
    branch_id: uuid.UUID | None
    department_id: uuid.UUID | None
    # Policies assigned to a User, loaded with the principal. Unused for other roles.
    assigned_policy_ids: frozenset[uuid.UUID] = frozenset()

    @property
    def permissions(self) -> frozenset[Permission]:
        return ROLE_PERMISSIONS[self.role]

    def has(self, permission: Permission) -> bool:
        return permission in self.permissions

    @property
    def is_tenant_user(self) -> bool:
        return self.organization_id is not None

    @property
    def reads_by_assignment(self) -> bool:
        """Users see only the policies assigned to them; other roles see by branch scope."""
        return self.role is Role.DEPARTMENT_USER

    @property
    def permission_scope(self) -> str:
        """Stable token identifying everything this principal can see (cache keys, audit).

        A User's assignments are part of it, so assigning or removing a policy
        never serves them an answer cached under their earlier access.
        """
        raw = f"{self.role}|{self.organization_id}|{self.branch_id}|{self.department_id}"
        if self.reads_by_assignment:
            raw += "|" + ",".join(sorted(str(p) for p in self.assigned_policy_ids))
        return hashlib.sha256(raw.encode()).hexdigest()[:32]
