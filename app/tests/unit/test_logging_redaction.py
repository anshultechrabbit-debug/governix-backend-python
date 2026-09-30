import pytest

from app.core.logging import redact


@pytest.mark.parametrize("raw, leaked", [
    ("postgresql+psycopg://governix:hunter2@localhost/db", "hunter2"),
    ("password=hunter2 user=x", "hunter2"),
    ('{"api_key": "abc123secret"}', "abc123secret"),
    ("Authorization: Bearer eyJhbGciOi.payload.sig", "eyJhbGciOi"),
    ("using key sk-proj-abcdefghijklmnopqrstuvwxyz", "abcdefghijklmnop"),
])
def test_secrets_are_redacted(raw, leaked):
    assert leaked not in redact(raw)


def test_ordinary_text_is_untouched():
    text = "Home Loan Credit Policy v4 effective 01 Jul 2026 LTV 75%"
    assert redact(text) == text
