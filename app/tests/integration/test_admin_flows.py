import pytest

from app.modules.auth.permissions import Role
from app.tests.factories import login, make_user

pytestmark = pytest.mark.integration


def test_full_onboarding_flow(client, db):
    master = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    platform = login(client, master)

    org = platform.post("/organizations", json={"name": "Axis Demo Bank", "slug": "axis-demo"})
    assert org.status_code == 201, org.text
    org_id = org.json()["data"]["id"]
    assert platform.post("/organizations", json={"name": "Dup", "slug": "axis-demo"}).json()[
        "error"]["code"] == "CONFLICT"

    platform.post("/users", json={
        "email": "admin@axis.example", "full_name": "Org Admin",
        "password": "long-enough-password", "role": "org_admin", "organization_id": org_id,
    })
    admin_tokens = client.post("/auth/login", json={
        "email": "admin@axis.example", "password": "long-enough-password",
    }).json()["data"]
    headers = {"Authorization": f"Bearer {admin_tokens['access_token']}"}

    branch = client.post("/branches", headers=headers, json={"name": "Mumbai HO", "code": "MUM"})
    assert branch.status_code == 201
    branch_id = branch.json()["data"]["id"]
    assert client.post("/branches", headers=headers, json={"name": "Dup", "code": "MUM"}).status_code == 409

    dept = client.post("/departments", headers=headers, json={
        "branch_id": branch_id, "name": "Credit", "code": "CRD",
    })
    assert dept.status_code == 201

    manager = client.post("/users", headers=headers, json={
        "email": "manager@axis.example", "full_name": "Branch Manager",
        "password": "long-enough-password", "role": "branch_manager", "branch_id": branch_id,
    })
    assert manager.status_code == 201, manager.text
    assert client.post("/users", headers=headers, json={
        "email": "MANAGER@axis.example", "full_name": "Dup", "password": "long-enough-password",
        "role": "branch_manager", "branch_id": branch_id,
    }).json()["error"]["code"] == "EMAIL_TAKEN"

    actions = {e["action"] for e in client.get("/audit-events", headers=headers).json()["data"]["items"]}
    assert {"branch.created", "department.created", "user.created"} <= actions


def test_role_change_requires_valid_scope(client, db):
    from app.tests.factories import make_tenant

    tenant = make_tenant(db)
    admin = login(client, tenant.admin)
    # Promoting a department user to branch manager clears the department.
    response = admin.patch(f"/users/{tenant.user_a1.id}", json={"role": "branch_manager"})
    assert response.status_code == 200
    assert response.json()["data"]["department_id"] is None
    # Moving a user to a department of a different branch is rejected.
    response = admin.patch(f"/users/{tenant.user_a2.id}", json={
        "department_id": str(tenant.dept_b1.id),
    })
    assert response.status_code == 422
    assert admin.patch(f"/users/{tenant.admin.id}", json={"is_active": False}).status_code == 422
