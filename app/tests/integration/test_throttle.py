import uuid

import pytest

from app.core.database import create_session_factory
from app.infrastructure.ai import throttle

pytestmark = pytest.mark.integration

PER_MINUTE = 600_000  # 9,000 usable tokens a second; a burst of 135,000


@pytest.fixture
def session_factory(db_engine):
    return create_session_factory(db_engine)


def test_requests_within_the_burst_go_at_once(session_factory):
    key = f"test:{uuid.uuid4()}"
    assert throttle.reserve(session_factory, key, 100_000, PER_MINUTE) == 0
    assert throttle.reserve(session_factory, key, 30_000, PER_MINUTE) == 0


def test_requests_beyond_the_budget_wait_in_turn(session_factory):
    key = f"test:{uuid.uuid4()}"
    throttle.reserve(session_factory, key, 135_000, PER_MINUTE)  # the whole burst
    first = throttle.reserve(session_factory, key, 90_000, PER_MINUTE)
    second = throttle.reserve(session_factory, key, 90_000, PER_MINUTE)
    assert first == pytest.approx(10, abs=0.5)
    assert second == pytest.approx(20, abs=0.5)


def test_no_limit_means_no_wait(session_factory):
    assert throttle.reserve(session_factory, f"test:{uuid.uuid4()}", 10**9, 0) == 0


def test_tokens_are_estimated_from_text_length():
    assert throttle.estimate_tokens(["a" * 400, "b" * 40]) == 110 + 2
