"""Only provider failures are resumed, and only once the provider is available."""
from app.modules.ingestion.recovery import PROVIDER_FAILURES, resume_provider_failures


class _NoQuery:
    def scalars(self, *_a, **_k):
        raise AssertionError("must not query when no failed provider is available again")


def test_nothing_is_resumed_while_the_provider_is_still_missing():
    assert resume_provider_failures(_NoQuery(), queue=None, available=set()) == []


def test_only_provider_errors_are_eligible():
    # Document-caused failures (NO_TEXT, PROCESSING_FAILED, ...) are never auto-retried.
    assert set(PROVIDER_FAILURES) == {"EMBEDDINGS_UNAVAILABLE", "OCR_UNAVAILABLE"}
