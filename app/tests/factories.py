"""Builders for a realistic multi-tenant world in integration tests."""

import uuid
from dataclasses import dataclass, field

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.modules.auth.permissions import Role
from app.modules.branches.model import Branch
from app.modules.categories.defaults import seed_default_categories
from app.modules.departments.model import Department
from app.modules.organizations.model import Organization
from app.modules.users.model import User

PASSWORD = "correct-horse-battery-staple"
_PASSWORD_HASH = hash_password(PASSWORD)  # hashing is deliberately slow; reuse it


def make_org(db: Session, slug: str | None = None) -> Organization:
    slug = slug or f"org-{uuid.uuid4().hex[:8]}"
    org = Organization(name=slug.title(), slug=slug)
    db.add(org)
    db.flush()
    seed_default_categories(db, org)
    return org


def make_branch(db: Session, org: Organization, code: str = "BR1") -> Branch:
    branch = Branch(organization_id=org.id, name=f"Branch {code}", code=code)
    db.add(branch)
    db.flush()
    return branch


def make_department(db: Session, branch: Branch, code: str = "CREDIT") -> Department:
    department = Department(
        organization_id=branch.organization_id, branch_id=branch.id, name=f"Dept {code}", code=code
    )
    db.add(department)
    db.flush()
    return department


def make_user(
    db: Session,
    role: Role,
    *,
    org: Organization | None = None,
    branch: Branch | None = None,
    department: Department | None = None,
    email: str | None = None,
) -> User:
    user = User(
        email=email or f"{role}-{uuid.uuid4().hex[:8]}@example.com",
        full_name=role.replace("_", " ").title(),
        password_hash=_PASSWORD_HASH,
        role=role,
        organization_id=org.id if org else None,
        branch_id=branch.id if branch else None,
        department_id=department.id if department else None,
    )
    db.add(user)
    db.flush()
    return user


@dataclass
class Tenant:
    org: Organization
    branch_a: Branch
    branch_b: Branch
    dept_a1: Department
    dept_a2: Department
    dept_b1: Department
    admin: User
    manager_a: User
    manager_b: User
    user_a1: User
    user_a2: User
    user_b1: User


def make_tenant(db: Session, slug: str | None = None) -> Tenant:
    org = make_org(db, slug)
    branch_a, branch_b = make_branch(db, org, "A"), make_branch(db, org, "B")
    dept_a1 = make_department(db, branch_a, "A1")
    dept_a2 = make_department(db, branch_a, "A2")
    dept_b1 = make_department(db, branch_b, "B1")
    tenant = Tenant(
        org=org, branch_a=branch_a, branch_b=branch_b,
        dept_a1=dept_a1, dept_a2=dept_a2, dept_b1=dept_b1,
        admin=make_user(db, Role.ORG_ADMIN, org=org),
        manager_a=make_user(db, Role.BRANCH_MANAGER, org=org, branch=branch_a),
        manager_b=make_user(db, Role.BRANCH_MANAGER, org=org, branch=branch_b),
        user_a1=make_user(db, Role.DEPARTMENT_USER, org=org, branch=branch_a, department=dept_a1),
        user_a2=make_user(db, Role.DEPARTMENT_USER, org=org, branch=branch_a, department=dept_a2),
        user_b1=make_user(db, Role.DEPARTMENT_USER, org=org, branch=branch_b, department=dept_b1),
    )
    db.commit()
    return tenant


@dataclass
class AuthClient:
    """Wraps TestClient with a user's bearer token."""

    client: TestClient
    user: User
    token: str = field(default="")

    def _headers(self, kwargs):
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def get(self, url, **kwargs):
        return self.client.get(url, headers=self._headers(kwargs), **kwargs)

    def post(self, url, **kwargs):
        return self.client.post(url, headers=self._headers(kwargs), **kwargs)

    def patch(self, url, **kwargs):
        return self.client.patch(url, headers=self._headers(kwargs), **kwargs)

    def put(self, url, **kwargs):
        return self.client.put(url, headers=self._headers(kwargs), **kwargs)

    def delete(self, url, **kwargs):
        return self.client.delete(url, headers=self._headers(kwargs), **kwargs)


def login(client: TestClient, user: User) -> AuthClient:
    response = client.post("/auth/login", json={"email": user.email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return AuthClient(client, user, response.json()["data"]["access_token"])


def category(db: Session, org: Organization, slug: str = "policies"):
    from sqlalchemy import select

    from app.modules.categories.model import Category

    return db.scalar(select(Category).where(Category.organization_id == org.id, Category.slug == slug))


def make_policy_from_document(
    db: Session,
    document_id,
    *,
    name: str,
    policy_number: str | None,
    label: str,
    effective_from,
    category_slug: str = "policies",
    issuer: str | None = "Credit Department",
):
    """Register an already-processed document as a confirmed policy version (bypasses review)."""
    from app.modules.documents.model import Document, DocumentStatus
    from app.modules.ingestion.analysis.metadata import normalize_title
    from app.modules.policies.model import Policy, PolicyVersion

    document = db.get(Document, document_id)
    org = db.get(Organization, document.organization_id)
    policy = Policy(
        organization_id=org.id, branch_id=document.branch_id, department_id=document.department_id,
        category_id=category(db, org, category_slug).id, name=name, normalized_name=normalize_title(name),
        policy_number=policy_number, issuer=issuer, issuing_department=issuer,
    )
    db.add(policy)
    db.flush()
    version = PolicyVersion(
        organization_id=org.id, policy_id=policy.id, document_id=document.id, version_number=1,
        version_label=label, effective_from=effective_from, content_hash=document.content_hash,
    )
    db.add(version)
    db.flush()
    document.policy_id, document.policy_version_id = policy.id, version.id
    document.status = DocumentStatus.READY
    db.commit()
    return policy, version


def attach_to_policy(db: Session, document_id, name: str | None = None):
    """Make an uploaded document part of a new policy at the document's scope (no version); returns the policy."""
    from app.modules.documents.model import Document
    from app.modules.ingestion.analysis.metadata import normalize_title
    from app.modules.policies.model import Policy

    document = db.get(Document, document_id)
    org = db.get(Organization, document.organization_id)
    name = name or f"Policy {uuid.uuid4().hex[:6]}"
    policy = Policy(
        organization_id=org.id, branch_id=document.branch_id, department_id=document.department_id,
        category_id=category(db, org).id, name=name, normalized_name=normalize_title(name),
    )
    db.add(policy)
    db.flush()
    document.policy_id = policy.id
    db.commit()
    return policy


def assign(db: Session, user: User, *policy_ids) -> None:
    """Assign policies to a User directly (bypasses the API's scope checks, to test the read side alone)."""
    from app.modules.assignments.model import PolicyAssignment

    for policy_id in policy_ids:
        db.add(PolicyAssignment(organization_id=user.organization_id, policy_id=policy_id, user_id=user.id))
    db.commit()
