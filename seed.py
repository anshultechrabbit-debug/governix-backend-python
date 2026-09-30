"""Database seeder script for Governix.

Usage:
    python seed.py
    # or with venv:
    .venv/bin/python seed.py
"""

from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.database import create_db_engine, create_session_factory
from app.core.model_registry import import_all_models
from app.core.security import hash_password
from app.modules.auth.permissions import Role
from app.modules.branches.model import Branch
from app.modules.categories.defaults import seed_default_categories
from app.modules.categories.model import Category
from app.modules.departments.model import Department
from app.modules.organizations.model import Organization
from app.modules.users.model import User

DEFAULT_PASSWORD = "Password@123456"


@dataclass
class SeedUser:
    email: str
    full_name: str
    password: str
    role: Role
    org_slug: str | None = None
    branch_code: str | None = None
    dept_code: str | None = None


def seed_database(session: Session, default_password: str = DEFAULT_PASSWORD) -> list[dict[str, str]]:
    password_hash = hash_password(default_password)
    seeded_credentials = []

    # 1. Master Admin (Global scope)
    master_admin_email = "master@governix.ai"
    master_admin = session.scalar(select(User).where(User.email == master_admin_email))
    if not master_admin:
        master_admin = User(
            email=master_admin_email,
            full_name="Governix Platform Master Admin",
            password_hash=password_hash,
            role=Role.MASTER_ADMIN,
            is_active=True,
        )
        session.add(master_admin)
        print(f"✓ Created Master Admin: {master_admin_email}")
    else:
        master_admin.password_hash = password_hash
        master_admin.is_active = True
        print(f"• Master Admin already exists (password updated): {master_admin_email}")

    seeded_credentials.append({
        "role": "Master Admin (Platform)",
        "name": "Governix Platform Master Admin",
        "email": master_admin_email,
        "password": default_password,
        "scope": "Global (All Organizations)",
    })

    # 2. Demo Organization
    org_slug = "acme-financial"
    org = session.scalar(select(Organization).where(Organization.slug == org_slug))
    if not org:
        org = Organization(
            name="Acme Financial Services",
            slug=org_slug,
        )
        session.add(org)
        session.flush()
        seed_default_categories(session, org)
        print(f"✓ Created Organization: {org.name} ({org_slug}) with default categories")
    else:
        print(f"• Organization already exists: {org.name} ({org_slug})")
        # Ensure default categories exist if missing
        cat_count = session.scalar(select(Category).where(Category.organization_id == org.id).limit(1))
        if not cat_count:
            seed_default_categories(session, org)
            print("✓ Seeded default categories for organization")

    # 3. Branches
    branches_data = [
        ("HQ", "Headquarters (Corporate Office)"),
        ("MUM", "Mumbai Regional Branch"),
    ]
    branches: dict[str, Branch] = {}
    for code, name in branches_data:
        branch = session.scalar(
            select(Branch).where(Branch.organization_id == org.id, Branch.code == code)
        )
        if not branch:
            branch = Branch(organization_id=org.id, name=name, code=code, is_active=True)
            session.add(branch)
            session.flush()
            print(f"✓ Created Branch: {name} [{code}]")
        else:
            print(f"• Branch already exists: {name} [{code}]")
        branches[code] = branch

    # 4. Departments
    dept_data = [
        ("HQ", "CREDIT", "Credit & Lending Policy"),
        ("HQ", "COMPL", "Compliance & Regulatory Affairs"),
        ("HQ", "RISK", "Enterprise Risk Management"),
        ("MUM", "RETAIL", "Retail Operations"),
        ("MUM", "OPS", "Branch Operations"),
    ]
    departments: dict[tuple[str, str], Department] = {}
    for branch_code, code, name in dept_data:
        branch = branches[branch_code]
        dept = session.scalar(
            select(Department).where(Department.branch_id == branch.id, Department.code == code)
        )
        if not dept:
            dept = Department(
                organization_id=org.id,
                branch_id=branch.id,
                name=name,
                code=code,
                is_active=True,
            )
            session.add(dept)
            session.flush()
            print(f"✓ Created Department: {name} [{code}] in Branch {branch_code}")
        else:
            print(f"• Department already exists: {name} [{code}]")
        departments[(branch_code, code)] = dept

    # 5. Users
    users_to_seed = [
        SeedUser(
            email="orgadmin@acme.com",
            full_name="Alice Admin",
            password=default_password,
            role=Role.ORG_ADMIN,
            org_slug=org_slug,
        ),
        SeedUser(
            email="branchmanager@acme.com",
            full_name="Bob Branch Manager",
            password=default_password,
            role=Role.BRANCH_MANAGER,
            org_slug=org_slug,
            branch_code="HQ",
        ),
        SeedUser(
            email="analyst@acme.com",
            full_name="Charlie Credit Analyst",
            password=default_password,
            role=Role.DEPARTMENT_USER,
            org_slug=org_slug,
            branch_code="HQ",
            dept_code="CREDIT",
        ),
        SeedUser(
            email="compliance@acme.com",
            full_name="Dana Compliance Officer",
            password=default_password,
            role=Role.DEPARTMENT_USER,
            org_slug=org_slug,
            branch_code="HQ",
            dept_code="COMPL",
        ),
        SeedUser(
            email="retail@acme.com",
            full_name="Evan Retail Officer",
            password=default_password,
            role=Role.DEPARTMENT_USER,
            org_slug=org_slug,
            branch_code="MUM",
            dept_code="RETAIL",
        ),
    ]

    for user_info in users_to_seed:
        branch = branches.get(user_info.branch_code) if user_info.branch_code else None
        dept = (
            departments.get((user_info.branch_code, user_info.dept_code))
            if user_info.branch_code and user_info.dept_code
            else None
        )

        user = session.scalar(select(User).where(User.email == user_info.email))
        if not user:
            user = User(
                email=user_info.email,
                full_name=user_info.full_name,
                password_hash=password_hash,
                role=user_info.role,
                organization_id=org.id,
                branch_id=branch.id if branch else None,
                department_id=dept.id if dept else None,
                is_active=True,
            )
            session.add(user)
            print(f"✓ Created User: {user_info.full_name} ({user_info.email}) [{user_info.role}]")
        else:
            user.password_hash = password_hash
            user.full_name = user_info.full_name
            user.role = user_info.role
            user.organization_id = org.id
            user.branch_id = branch.id if branch else None
            user.department_id = dept.id if dept else None
            user.is_active = True
            print(f"• Updated User: {user_info.full_name} ({user_info.email}) [{user_info.role}]")

        scope_desc = org.name
        if branch:
            scope_desc += f" > {branch.name}"
        if dept:
            scope_desc += f" > {dept.name}"

        seeded_credentials.append({
            "role": str(user_info.role),
            "name": user_info.full_name,
            "email": user_info.email,
            "password": user_info.password,
            "scope": scope_desc,
        })

    session.commit()
    return seeded_credentials


def main() -> None:
    print("=" * 80)
    print("  Governix Database Seeder")
    print("=" * 80)

    import_all_models()
    settings = get_settings()
    engine = create_db_engine(settings)
    session_factory = create_session_factory(engine)

    with session_factory() as session:
        credentials = seed_database(session)

    engine.dispose()

    print("\n" + "=" * 80)
    print("  SEEDED CREDENTIALS & TEST USERS")
    print("=" * 80)
    col_fmt = "{:<20} | {:<25} | {:<16} | {:<30}"
    print(col_fmt.format("Role", "Email", "Password", "Scope"))
    print("-" * 100)
    for c in credentials:
        print(col_fmt.format(c["role"], c["email"], c["password"], c["scope"]))
    print("=" * 100)
    print("\n✓ Database seeding completed successfully!\n")


if __name__ == "__main__":
    main()
