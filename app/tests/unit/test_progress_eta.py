from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.modules.ingestion.model import StageStatus
from app.modules.ingestion.progress import _eta_seconds


def stage(done, total, started_minutes_ago, window=None):
    now = datetime.now(UTC)
    return SimpleNamespace(status=StageStatus.RUNNING, total_units=total, done_units=done,
                           started_at=now - timedelta(minutes=started_minutes_ago),
                           detail={"rate_window": window} if window else {}), now


def test_the_estimate_follows_the_recent_pace_not_the_average():
    # 30,000 done in 10 minutes (50/s average), but the last minute ran at 130/s.
    now = datetime.now(UTC)
    row, now = stage(30_000, 139_000, 10, [[(now - timedelta(seconds=60)).isoformat(), 30_000 - 7_800]])
    eta = _eta_seconds(row, now)
    assert 800 <= eta <= 900  # 109,000 left at 130/s ~ 14 minutes, not ~36 at the average


def test_without_recent_samples_the_average_is_used():
    row, now = stage(30_000, 139_000, 10)
    assert 2100 <= _eta_seconds(row, now) <= 2250


def test_time_waiting_in_the_queue_is_not_counted_as_slow_extraction():
    # Queued 10 minutes ago behind other documents; extraction began a minute ago (its first
    # sample) and has done 50 of 1,000 pages: about 19 minutes left, not over 3 hours.
    now = datetime.now(UTC)
    row, now = stage(50, 1_000, 10, [[(now - timedelta(seconds=60)).isoformat(), 0]])
    assert 1100 <= _eta_seconds(row, now) <= 1180
