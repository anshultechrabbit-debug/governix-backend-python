import os

import pytest

from app.workers import __main__ as worker_main


@pytest.fixture
def priority(monkeypatch):
    """A fake process priority, so the test run itself is never reniced."""
    state = {"nice": 0, "allowed": True}

    def setpriority(_which, _who, value):
        if not state["allowed"]:
            raise PermissionError("not permitted")
        state["nice"] = value

    monkeypatch.setattr(os, "getpriority", lambda _which, _who: state["nice"], raising=False)
    monkeypatch.setattr(os, "setpriority", setpriority, raising=False)
    monkeypatch.setattr(os, "PRIO_PROCESS", 0, raising=False)
    return state


def test_the_worker_lowers_its_priority(priority):
    assert worker_main.lower_priority(10) == 10


def test_a_lower_priority_set_by_the_service_is_kept(priority):
    # Started with `nice -n 15` (or systemd Nice=15): the setting never raises it back to 10.
    priority["nice"] = 15
    assert worker_main.lower_priority(10) == 15


def test_zero_leaves_the_priority_unchanged(priority):
    assert worker_main.lower_priority(0) == 0


def test_out_of_range_values_are_clamped(priority):
    assert worker_main.lower_priority(40) == 19


def test_a_refused_change_does_not_stop_the_worker(priority):
    priority["allowed"] = False
    assert worker_main.lower_priority(10) == 0
