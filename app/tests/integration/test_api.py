import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.exceptions import ConflictError
from app.main import create_app

pytestmark = pytest.mark.integration


class Payload(BaseModel):
    count: int


@pytest.fixture
def client(settings):
    app = create_app(settings)
    probe = APIRouter()

    @probe.get("/_boom")
    def boom():
        raise RuntimeError("secret internal detail")

    @probe.get("/_conflict")
    def conflict():
        raise ConflictError(
            "The uploaded document conflicts with an existing version.", code="VERSION_CONFLICT"
        )

    @probe.post("/_validate")
    def validate(body: Payload):
        return {}

    app.include_router(probe)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def test_health_envelope_and_request_id(client):
    response = client.get("/health")
    body = response.json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["data"]["status"] == "OK"
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_db_health(client):
    assert client.get("/db-health").json()["data"] == {"status": "OK", "database": "connected"}


def test_valid_client_request_id_is_propagated(client):
    response = client.get("/health", headers={"X-Request-ID": "trace-abc-12345"})
    assert response.headers["X-Request-ID"] == "trace-abc-12345"
    assert response.json()["request_id"] == "trace-abc-12345"


def test_malformed_request_id_is_replaced(client):
    response = client.get("/health", headers={"X-Request-ID": "bad id\n<script>"})
    assert response.headers["X-Request-ID"] != "bad id\n<script>"


def test_app_error_envelope(client):
    response = client.get("/_conflict")
    body = response.json()
    assert response.status_code == 409
    assert body["success"] is False
    assert body["error"]["code"] == "VERSION_CONFLICT"
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_not_found_envelope(client):
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_validation_error_does_not_echo_input(client):
    response = client.post("/_validate", json={"count": "not-a-number-SECRET"})
    body = response.json()
    assert response.status_code == 422
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"][0]["field"] == "body.count"
    assert "SECRET" not in response.text


def test_unhandled_error_hides_internals(client):
    response = client.get("/_boom")
    body = response.json()
    assert response.status_code == 500
    assert body["error"] == {"code": "INTERNAL_ERROR", "message": "An unexpected error occurred."}
    assert "secret internal detail" not in response.text
    assert "Traceback" not in response.text
