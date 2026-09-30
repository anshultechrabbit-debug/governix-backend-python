import pytest

from app.modules.auth.permissions import Role
from app.tests.factories import login, make_tenant, make_user

pytestmark = pytest.mark.integration


@pytest.fixture
def world(db):
    return make_tenant(db, "bank-a"), make_tenant(db, "bank-b")


def ids(response):
    return {item["id"] for item in response.json()["data"]["items"]}


def test_organization_a_cannot_see_organization_b(client, world):
    bank_a, bank_b = world
    admin_a = login(client, bank_a.admin)
    assert str(bank_b.branch_a.id) not in ids(admin_a.get("/branches"))
    assert admin_a.get(f"/branches/{bank_b.branch_a.id}").status_code == 404
    assert admin_a.get(f"/departments/{bank_b.dept_a1.id}").status_code == 404
    assert admin_a.get(f"/users/{bank_b.user_a1.id}").status_code == 404
    assert str(bank_b.user_a1.id) not in ids(admin_a.get("/users"))
    assert admin_a.patch(f"/users/{bank_b.user_a1.id}", json={"is_active": False}).status_code == 404


def test_org_admin_cannot_attach_users_to_another_orgs_branch(client, world):
    bank_a, bank_b = world
    response = login(client, bank_a.admin).post("/users", json={
        "email": "x@example.com", "full_name": "X", "password": "long-enough-password",
        "role": "department_user", "branch_id": str(bank_b.branch_a.id),
        "department_id": str(bank_b.dept_a1.id),
    })
    assert response.status_code == 422


def test_branch_manager_is_confined_to_their_branch(client, world):
    bank_a, _ = world
    manager = login(client, bank_a.manager_a)

    assert ids(manager.get("/branches")) == {str(bank_a.branch_a.id)}
    assert manager.get(f"/branches/{bank_a.branch_b.id}").status_code == 404
    visible_users = ids(manager.get("/users"))
    assert str(bank_a.user_a1.id) in visible_users
    assert str(bank_a.user_b1.id) not in visible_users
    assert str(bank_a.manager_b.id) not in visible_users

    # Cannot create departments or users in branch B, nor promote anyone.
    assert manager.post("/departments", json={
        "branch_id": str(bank_a.branch_b.id), "name": "Ops", "code": "OPS",
    }).status_code == 403
    assert manager.patch(
        f"/users/{bank_a.user_a1.id}", json={"role": "branch_manager"}
    ).status_code == 403
    assert manager.patch(f"/users/{bank_a.user_b1.id}", json={"is_active": False}).status_code == 404


def test_branch_manager_creates_user_in_own_branch_regardless_of_input(client, world):
    bank_a, _ = world
    response = login(client, bank_a.manager_a).post("/users", json={
        "email": "new.user@example.com", "full_name": "New User",
        "password": "long-enough-password", "role": "department_user",
        "branch_id": str(bank_a.branch_b.id),  # ignored: forced to the manager's branch
        "department_id": str(bank_a.dept_a2.id),
    })
    assert response.status_code == 201, response.text
    assert response.json()["data"]["branch_id"] == str(bank_a.branch_a.id)


def test_department_user_cannot_manage_anything(client, world):
    bank_a, _ = world
    user = login(client, bank_a.user_a1)
    assert user.get("/users").status_code == 403
    assert user.post("/branches", json={"name": "X", "code": "X"}).status_code == 403
    assert user.post("/departments", json={
        "branch_id": str(bank_a.branch_a.id), "name": "Ops", "code": "OPS",
    }).status_code == 403
    assert user.get("/audit-events").status_code == 403
    assert user.get(f"/users/{user.user.id}").status_code == 200  # own profile


def test_org_admin_audit_log_is_tenant_scoped(client, world, db):
    bank_a, bank_b = world
    login(client, bank_b.admin).post("/branches", json={"name": "Secret Branch", "code": "SEC"})
    events = login(client, bank_a.admin).get("/audit-events").json()["data"]["items"]
    assert all(e["organization_id"] == str(bank_a.org.id) for e in events)
    assert not any(e["action"] == "branch.created" for e in events)


def test_master_admin_creates_org_admin_only(client, db, world):
    bank_a, _ = world
    master = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    session = login(client, master)
    base = {"email": "boss@example.com", "full_name": "Boss", "password": "long-enough-password",
            "organization_id": str(bank_a.org.id)}
    assert session.post("/users", json={**base, "role": "org_admin"}).status_code == 201
    assert session.post("/users", json={
        **base, "email": "b2@example.com", "role": "branch_manager",
        "branch_id": str(bank_a.branch_a.id),
    }).status_code == 403
