"""Complaints and support tickets by level; organisation onboarding and branding."""

import io

import pytest

from app.modules.auth.permissions import Role
from app.tests.factories import login, make_tenant, make_user

pytestmark = pytest.mark.integration

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture
def tenant(db):
    return make_tenant(db)


@pytest.fixture
def master(db):
    user = make_user(db, Role.MASTER_ADMIN)
    db.commit()
    return user


def raise_ticket(session, **body):
    response = session.post("/tickets", json={"subject": "Branch service issue", "description": "Queue was not managed.", **body})
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_user_complaint_reaches_only_their_branch_manager(client, tenant):
    user = login(client, tenant.user_a1)
    ticket = raise_ticket(user, kind="complaint", category="Service")
    assert (ticket["level"], ticket["status"], ticket["kind"]) == ("branch", "open", "complaint")

    manager_a, manager_b = login(client, tenant.manager_a), login(client, tenant.manager_b)
    queue = manager_a.get("/tickets?box=queue").json()["data"]["items"]
    assert [t["id"] for t in queue] == [ticket["id"]] and queue[0]["can_handle"] is True
    assert manager_a.get("/notifications").json()["data"]["items"][0]["type"] == "TICKET_UPDATED"
    assert manager_b.get(f"/tickets/{ticket['id']}").status_code == 404
    assert login(client, tenant.admin).get(f"/tickets/{ticket['id']}").status_code == 404  # organisation level only
    assert login(client, tenant.user_a2).get(f"/tickets/{ticket['id']}").status_code == 404

    # The manager's reply moves it along and notifies the user as a complaint response.
    replied = manager_a.post(f"/tickets/{ticket['id']}/messages", json={"body": "We have added a second counter."})
    assert replied.json()["data"]["status"] == "in_progress"
    assert user.get("/notifications").json()["data"]["items"][0]["type"] == "COMPLAINT_RESPONSE"
    resolved = manager_a.post(f"/tickets/{ticket['id']}/status", json={"status": "resolved", "note": "Fixed"})
    assert resolved.json()["data"]["status"] == "resolved" and resolved.json()["data"]["resolved_at"]

    # The user may accept (close) or reopen, but not e.g. set it in progress.
    assert user.post(f"/tickets/{ticket['id']}/status", json={"status": "in_progress"}).status_code == 403
    closed = user.post(f"/tickets/{ticket['id']}/status", json={"status": "closed"}).json()["data"]
    assert closed["status"] == "closed"
    assert user.post(f"/tickets/{ticket['id']}/messages", json={"body": "one more thing"}).status_code == 409
    history = [m["status_change"] for m in closed["messages"] if m["status_change"]]
    assert history == ["in_progress", "resolved", "closed"]


def test_support_escalates_by_level(client, tenant, master):
    manager_ticket = raise_ticket(login(client, tenant.manager_a), subject="Need a new category")
    assert manager_ticket["level"] == "organization"
    admin = login(client, tenant.admin)
    assert [t["id"] for t in admin.get("/tickets?box=queue").json()["data"]["items"]] == [manager_ticket["id"]]

    admin_ticket = raise_ticket(admin, subject="Platform is slow")
    assert admin_ticket["level"] == "platform"
    platform = login(client, master)
    assert [t["id"] for t in platform.get("/tickets?box=queue").json()["data"]["items"]] == [admin_ticket["id"]]
    assert platform.get(f"/tickets/{manager_ticket['id']}").status_code == 404

    assert login(client, tenant.manager_a).post("/tickets", json={
        "kind": "complaint", "subject": "x" * 5, "description": "only users complain",
    }).status_code == 422
    assert platform.post("/tickets", json={"subject": "hello", "description": "masters do not raise"}).status_code == 403


def test_ticket_attachments(client, tenant):
    user = login(client, tenant.user_a1)
    ticket = raise_ticket(user)
    sent = user.post(f"/tickets/{ticket['id']}/attachments",
                     files={"file": ("receipt.png", io.BytesIO(PNG), "image/png")})
    assert sent.status_code == 201, sent.text
    [attachment] = sent.json()["data"]["attachments"]
    download = login(client, tenant.manager_a).get(f"/tickets/{ticket['id']}/attachments/{attachment['id']}")
    assert download.status_code == 200 and download.content == PNG
    assert user.post(f"/tickets/{ticket['id']}/attachments",
                     files={"file": ("run.exe", io.BytesIO(b"MZ"), "application/octet-stream")}).status_code == 422
    assert login(client, tenant.manager_b).get(f"/tickets/{ticket['id']}/attachments/{attachment['id']}").status_code == 404


def test_organisation_is_created_with_branding_and_its_first_admin(client, db, master):
    platform = login(client, master)
    created = platform.post("/organizations", json={
        "name": "ABC Bank", "slug": "abc-bank", "primary_color": "#1e3a8a", "contact_email": "it@abcbank.com",
        "admin": {"email": "admin@abcbank.com", "full_name": "ABC Admin", "password": "a-very-long-password-1"},
    })
    assert created.status_code == 201, created.text
    org = created.json()["data"]
    assert (org["primary_color"], org["contact_email"], org["has_logo"]) == ("#1e3a8a", "it@abcbank.com", False)
    assert platform.post(f"/organizations/{org['id']}/logo",
                         files={"file": ("logo.png", io.BytesIO(PNG), "image/png")}).json()["data"]["has_logo"] is True

    session = client.post("/auth/login", json={"email": "admin@abcbank.com", "password": "a-very-long-password-1"})
    assert session.status_code == 200, session.text
    headers = {"Authorization": f"Bearer {session.json()['data']['access_token']}"}
    mine = client.get("/organizations/me", headers=headers).json()["data"]
    assert mine["name"] == "ABC Bank" and mine["has_logo"] is True
    assert client.get(f"/organizations/{org['id']}/logo", headers=headers).content == PNG

    duplicate_admin = platform.post("/organizations", json={
        "name": "XYZ Bank", "slug": "xyz-bank",
        "admin": {"email": "admin@abcbank.com", "full_name": "Dup", "password": "a-very-long-password-1"},
    })
    assert duplicate_admin.status_code == 422
    assert platform.get("/organizations").json()["data"]["total"] == 1  # nothing half-created
    assert platform.patch(f"/organizations/{org['id']}", json={"primary_color": "blue"}).status_code == 422


def test_other_organisations_cannot_read_a_logo(client, db, tenant, master):
    other = make_tenant(db)
    platform = login(client, master)
    platform.post(f"/organizations/{other.org.id}/logo", files={"file": ("logo.png", io.BytesIO(PNG), "image/png")})
    assert login(client, tenant.admin).get(f"/organizations/{other.org.id}/logo").status_code == 404


def test_branch_manager_creates_users_without_a_department(client, tenant):
    response = login(client, tenant.manager_a).post("/users", json={
        "email": "rahul@example.com", "full_name": "Rahul", "password": "a-very-long-password-1",
        "role": "department_user",
    })
    assert response.status_code == 201, response.text
    user = response.json()["data"]
    assert user["branch_id"] == str(tenant.branch_a.id) and user["department_id"] is None
