"""Chat history: each answer is saved to its asker's conversation, and only they can read it."""

import json

import pytest

from app.tests.factories import login, make_tenant
from app.tests.flows import build, confirm_new_policy, home_loan_spec, process
from app.tests.pipeline import drain

pytestmark = pytest.mark.integration

QUESTION = "What is the LTV for home loans above 75 lakh?"


@pytest.fixture
def tenant(db):
    return make_tenant(db)


@pytest.fixture
def admin(client, tenant):
    return login(client, tenant.admin)


@pytest.fixture
def home_loan(client, app, tenant, admin):
    document, analysis = process(client, app, tenant.admin, build(home_loan_spec("1", "01/01/2024")))
    assert confirm_new_policy(admin, document, analysis).status_code == 200
    drain(app)


def ask(session, question, **body):
    response = session.post("/ai/ask", json={"question": question, **body})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_an_answer_starts_a_conversation_and_later_questions_join_it(admin, home_loan):
    first = ask(admin, QUESTION)
    conversation_id = first["conversation_id"]
    assert conversation_id
    second = ask(admin, "What is the interest rate spread?", conversation_id=conversation_id,
                 history=[{"question": QUESTION, "answer": first["answer"]}])
    assert second["conversation_id"] == conversation_id

    [listed] = admin.get("/ai/conversations").json()["data"]["items"]
    assert (listed["id"], listed["title"], listed["message_count"]) == (conversation_id, QUESTION, 2)
    detail = admin.get(f"/ai/conversations/{conversation_id}").json()["data"]
    assert [m["question"] for m in detail["messages"]] == [QUESTION, "What is the interest rate spread?"]
    assert detail["messages"][0]["answer"]["claims"] == first["claims"]


def test_a_new_question_without_a_conversation_starts_another(admin, home_loan):
    ask(admin, QUESTION)
    ask(admin, "What is the interest rate spread?")
    assert admin.get("/ai/conversations").json()["data"]["total"] == 2


def test_the_streamed_answer_is_saved_too(admin, home_loan):
    response = admin.post("/ai/ask/stream", json={"question": QUESTION})
    done = next(
        json.loads(block.split("data: ", 1)[1]) for block in response.text.split("\n\n") if block.startswith("event: done")
    )
    assert done["conversation_id"]
    assert admin.get(f"/ai/conversations/{done['conversation_id']}").json()["data"]["message_count"] == 1


def test_conversations_are_private(client, tenant, admin, home_loan):
    conversation_id = ask(admin, QUESTION)["conversation_id"]
    other = login(client, tenant.manager_a)
    assert other.get("/ai/conversations").json()["data"]["total"] == 0
    assert other.get(f"/ai/conversations/{conversation_id}").status_code == 404
    assert other.delete(f"/ai/conversations/{conversation_id}").status_code == 404
    # Asking "into" someone else's conversation starts the asker's own instead.
    mine = other.post("/ai/ask", json={"question": QUESTION, "conversation_id": conversation_id}).json()["data"]
    assert mine["conversation_id"] != conversation_id
    assert admin.get(f"/ai/conversations/{conversation_id}").json()["data"]["message_count"] == 1


def test_rename_delete_and_clear(admin, home_loan):
    first = ask(admin, QUESTION)["conversation_id"]
    ask(admin, "What is the interest rate spread?")
    renamed = admin.patch(f"/ai/conversations/{first}", json={"title": "  LTV   limits "}).json()["data"]
    assert renamed["title"] == "LTV limits"
    assert admin.delete(f"/ai/conversations/{first}").status_code == 200
    assert admin.get(f"/ai/conversations/{first}").status_code == 404
    assert admin.delete("/ai/conversations").json()["data"] == {"deleted": 1}
    assert admin.get("/ai/conversations").json()["data"]["total"] == 0
