from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.modules.audit.model import AuditEvent
from app.modules.auth.permissions import Role
from app.modules.organizations.model import OrganizationStatus
from app.tests.factories import PASSWORD, login, make_org, make_tenant, make_user

pytestmark = pytest.mark.integration


def test_login_me_and_permissions(client, db):
    tenant = make_tenant(db)
    session = login(client, tenant.user_a1)
    me = session.get("/auth/me").json()["data"]
    assert me["email"] == tenant.user_a1.email
    assert me["organization_id"] == str(tenant.org.id)
    assert "ai:query" in me["permissions"]
    assert "users:manage" not in me["permissions"]


def test_login_is_case_insensitive_on_email(client, db):
    tenant = make_tenant(db)
    response = client.post(
        "/auth/login", json={"email": tenant.admin.email.upper(), "password": PASSWORD}
    )
    assert response.status_code == 200


@pytest.mark.parametrize("email", ["nobody@example.com", None])
def test_bad_credentials_give_identical_error(client, db, email):
    tenant = make_tenant(db)
    response = client.post(
        "/auth/login", json={"email": email or tenant.admin.email, "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": "INVALID_CREDENTIALS", "message": "Invalid email or password.",
    }


def test_lockout_after_repeated_failures(client, db, settings):
    tenant = make_tenant(db)
    for _ in range(settings.LOGIN_MAX_FAILED_ATTEMPTS):
        client.post("/auth/login", json={"email": tenant.admin.email, "password": "nope"})
    response = client.post("/auth/login", json={"email": tenant.admin.email, "password": PASSWORD})
    assert response.json()["error"]["code"] == "ACCOUNT_LOCKED"

    db.refresh(tenant.admin)
    tenant.admin.locked_until = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert client.post(
        "/auth/login", json={"email": tenant.admin.email, "password": PASSWORD}
    ).status_code == 200


def test_missing_and_malformed_tokens(client):
    assert client.get("/auth/me").json()["error"]["code"] == "UNAUTHENTICATED"
    response = client.get("/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_TOKEN"


def test_refresh_rotates_and_detects_reuse(client, db):
    tenant = make_tenant(db)
    tokens = client.post(
        "/auth/login", json={"email": tenant.admin.email, "password": PASSWORD}
    ).json()["data"]

    rotated = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert rotated.status_code == 200
    new_refresh = rotated.json()["data"]["refresh_token"]
    assert new_refresh != tokens["refresh_token"]

    # Replaying the old token is treated as theft: the whole family is revoked.
    reuse = client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert reuse.status_code == 401
    assert client.post("/auth/refresh", json={"refresh_token": new_refresh}).status_code == 401
    assert db.scalar(select(AuditEvent).where(AuditEvent.action == "auth.refresh_token_reuse"))


def test_logout_revokes_refresh_token(client, db):
    tenant = make_tenant(db)
    tokens = client.post(
        "/auth/login", json={"email": tenant.admin.email, "password": PASSWORD}
    ).json()["data"]
    client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert client.post(
        "/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    ).status_code == 401


def test_deactivation_invalidates_existing_access_tokens_immediately(client, db):
    tenant = make_tenant(db)
    user_session = login(client, tenant.user_a1)
    admin = login(client, tenant.admin)
    assert admin.patch(f"/users/{tenant.user_a1.id}", json={"is_active": False}).status_code == 200
    assert user_session.get("/auth/me").status_code == 401


def test_suspended_organization_blocks_its_users(client, db):
    tenant = make_tenant(db)
    master = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    user_session = login(client, tenant.user_a1)
    response = login(client, master).patch(
        f"/organizations/{tenant.org.id}", json={"status": OrganizationStatus.SUSPENDED}
    )
    assert response.status_code == 200
    assert user_session.get("/auth/me").json()["error"]["code"] == "ORGANIZATION_SUSPENDED"
    assert client.post(
        "/auth/login", json={"email": tenant.user_a1.email, "password": PASSWORD}
    ).status_code == 401


def test_change_password_ends_all_sessions(client, db):
    tenant = make_tenant(db)
    session = login(client, tenant.user_a1)
    new_password = "another-long-password-42"
    assert session.post(
        "/auth/change-password",
        json={"current_password": PASSWORD, "new_password": new_password},
    ).status_code == 200
    assert session.get("/auth/me").status_code == 401
    assert client.post(
        "/auth/login", json={"email": tenant.user_a1.email, "password": new_password}
    ).status_code == 200


def test_master_admin_has_no_tenant_data_access(client, db):
    make_org(db)
    master = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    session = login(client, master)
    assert session.get("/branches").status_code == 403
    assert session.get("/audit-events").status_code == 403
