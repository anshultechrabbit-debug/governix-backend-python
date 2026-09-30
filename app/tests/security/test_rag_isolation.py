import pytest
from sqlalchemy import select

from app.modules.audit.model import AuditEvent
from app.modules.search.model import Chunk
from app.tests.factories import assign, login, make_tenant
from app.tests.flows import build, confirm_new_policy, process
from app.tests.pdfs import PolicySpec, Section
from app.tests.pipeline import drain

pytestmark = pytest.mark.integration

TREASURY = PolicySpec(
    title="TREASURY LIMITS POLICY",
    header_lines=["Policy No: TR-2026-07", "Effective Date: 01/01/2026"],
    sections=[Section("1", "Dealer Limits", ["The overnight dealer limit is Rs. 50 crore per desk."])],
)


@pytest.fixture
def world(client, app, db):
    bank_a, bank_b = make_tenant(db, "bank-a"), make_tenant(db, "bank-b")
    doc, analysis = process(client, app, bank_a.admin, build(TREASURY),
                            branch_id=bank_a.branch_a.id, department_id=bank_a.dept_a2.id)
    policy_id = confirm_new_policy(login(client, bank_a.manager_a), doc, analysis).json()["data"]["policy_id"]
    drain(app)
    assign(db, bank_a.user_a2, policy_id)
    restricted = set(db.scalars(select(Chunk.id).where(Chunk.document_id == doc)))
    return bank_a, bank_b, restricted


def ask(client, user, question):
    return login(client, user).post("/ai/ask", json={"question": question}).json()["data"]


def test_unauthorised_text_never_reaches_ai_context_or_citations(client, db, world):
    bank_a, bank_b, restricted = world
    assert ask(client, bank_a.user_a2, "What is the overnight dealer limit per desk?")["status"] == "answered"

    for outsider in (bank_a.user_a1, bank_a.user_b1, bank_a.manager_b, bank_b.admin):
        answer = ask(client, outsider, "What is the overnight dealer limit per desk?")
        assert answer["status"] == "no_answer"
        assert "50 crore" not in str(answer)
        event = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "ai.query", AuditEvent.actor_user_id == outsider.id)
        ).first()
        assert not ({str(c) for c in restricted} & set(event.details["retrieved_chunk_ids"]))


def test_answer_cache_cannot_leak_between_departments(client, world):
    bank_a, _, _ = world
    question = "What is the overnight dealer limit per desk?"
    assert ask(client, bank_a.user_a2, question)["status"] == "answered"
    leaked = ask(client, bank_a.user_a1, question)
    assert leaked["cache_hit"] is False and leaked["status"] == "no_answer"


def test_unassigned_policy_is_never_retrieved_for_a_user(client, db, world):
    """Spec §54: the policy exists in the same organisation and scope, but is not assigned."""
    bank_a, _, restricted = world
    from app.modules.auth.permissions import Role
    from app.tests.factories import make_user

    colleague = make_user(db, Role.DEPARTMENT_USER, org=bank_a.org, branch=bank_a.branch_a, department=bank_a.dept_a2)
    db.commit()
    answer = ask(client, colleague, "What is the overnight dealer limit per desk?")
    assert answer["status"] == "no_answer" and "50 crore" not in str(answer)


def test_assignment_takes_effect_and_invalidates_cached_answers(client, db, world):
    bank_a, _, _ = world
    from app.modules.auth.permissions import Role
    from app.modules.policies.model import Policy
    from app.tests.factories import make_user

    colleague = make_user(db, Role.DEPARTMENT_USER, org=bank_a.org, branch=bank_a.branch_a, department=bank_a.dept_a2)
    db.commit()
    question = "What is the overnight dealer limit per desk?"
    assert ask(client, colleague, question)["status"] == "no_answer"
    policy_id = db.scalar(select(Policy.id).where(Policy.organization_id == bank_a.org.id))
    manager = login(client, bank_a.manager_a)
    assert manager.post(f"/policies/{policy_id}/assignments", json={"user_ids": [str(colleague.id)]}).status_code == 200
    answered = ask(client, colleague, question)
    assert answered["status"] == "answered" and answered["cache_hit"] is False
    assert manager.delete(f"/policies/{policy_id}/assignments/{colleague.id}").status_code == 200
    assert ask(client, colleague, question)["status"] == "no_answer"
