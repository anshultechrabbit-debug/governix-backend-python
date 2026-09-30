import uuid

from app.modules.rag.service import _without_file_names
from app.modules.search.retrieval import NamedDocument, names_file


def test_the_whole_file_name_is_matched():
    assert names_file("What type of document is described in policy_document.pdf?", "policy_document.pdf")
    assert names_file("Summarise POLICY_DOCUMENT.PDF", "policy_document.pdf")
    assert not names_file("What does old_policy_document.pdf say?", "policy_document.pdf")
    assert not names_file("What does data.pdf say?", "a.pdf")


def test_the_file_name_is_replaced_before_searching():
    named = [NamedDocument(uuid.uuid4(), uuid.uuid4(), "policy_v2.pdf")]
    assert _without_file_names("What is the LTV in policy_v2.pdf?", named) == "What is the LTV in the document?"
