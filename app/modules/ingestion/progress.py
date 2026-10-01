"""Ingestion progress bookkeeping.

Progress is derived from real unit counts (pages extracted, chunks embedded).
The ETA is only reported once enough units have completed to measure a rate,
and is always labelled an estimate by the API.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.modules.ingestion.model import STAGE_ORDER, IngestionStage, Stage, StageStatus

MIN_UNITS_FOR_ETA = 10
MIN_FRACTION_FOR_ETA = 0.02


def init_stages(session: Session, document_id: uuid.UUID) -> None:
    for stage in STAGE_ORDER:
        session.add(IngestionStage(document_id=document_id, stage=stage, status=StageStatus.PENDING))
    session.flush()


def _where(document_id: uuid.UUID, stage: Stage):
    return (IngestionStage.document_id == document_id, IngestionStage.stage == stage)


def start(
    session: Session, document_id: uuid.UUID, stage: Stage, *, total_units: int | None = None
) -> None:
    values: dict[str, Any] = {
        "status": StageStatus.RUNNING,
        "started_at": datetime.now(UTC),
        "finished_at": None,
    }
    if total_units is not None:
        values["total_units"] = total_units
        values["done_units"] = 0
    session.execute(update(IngestionStage).where(*_where(document_id, stage)).values(**values))


def wait(session: Session, document_id: uuid.UUID, stage: Stage, detail: dict[str, Any] | None = None) -> None:
    """The stage is blocked on a person (e.g. confirming the analysis)."""
    values: dict[str, Any] = {"status": StageStatus.WAITING, "started_at": datetime.now(UTC), "finished_at": None}
    if detail is not None:
        values["detail"] = detail
    session.execute(update(IngestionStage).where(*_where(document_id, stage)).values(**values))


def set_total(session: Session, document_id: uuid.UUID, stage: Stage, total_units: int) -> None:
    session.execute(
        update(IngestionStage).where(*_where(document_id, stage)).values(total_units=total_units)
    )


def advance(session: Session, document_id: uuid.UUID, stage: Stage, units: int) -> None:
    """Atomically add completed units (safe with many concurrent workers)."""
    session.execute(
        update(IngestionStage)
        .where(*_where(document_id, stage))
        .values(done_units=IngestionStage.done_units + units)
    )


# The time left is estimated from the recent pace, not the average since the stage began:
# after a restart or a change of worker count, the average lags for a long time.
RATE_SAMPLE_SECONDS = 15
RATE_SAMPLES = 6  # about the last 90 seconds
MIN_RATE_WINDOW_SECONDS = 20


def set_done(session: Session, document_id: uuid.UUID, stage: Stage, units: int) -> None:
    detail = session.scalar(select(IngestionStage.detail).where(*_where(document_id, stage))) or {}
    now = datetime.now(UTC)
    window = list(detail.get("rate_window") or [])
    values: dict[str, Any] = {"done_units": units}
    if not window or (now - datetime.fromisoformat(window[-1][0])).total_seconds() >= RATE_SAMPLE_SECONDS:
        values["detail"] = {**detail, "rate_window": (window + [[now.isoformat(), units]])[-RATE_SAMPLES:]}
    session.execute(update(IngestionStage).where(*_where(document_id, stage)).values(**values))


def finish(
    session: Session,
    document_id: uuid.UUID,
    stage: Stage,
    status: StageStatus = StageStatus.COMPLETED,
    detail: dict[str, Any] | None = None,
) -> None:
    values: dict[str, Any] = {"status": status, "finished_at": datetime.now(UTC)}
    if detail is not None:
        values["detail"] = detail
    session.execute(update(IngestionStage).where(*_where(document_id, stage)).values(**values))


def reset_from(session: Session, document_id: uuid.UUID, stage: Stage) -> None:
    """Mark `stage` and every later stage pending again (retry / re-index)."""
    later = STAGE_ORDER[STAGE_ORDER.index(stage):]
    session.execute(
        update(IngestionStage)
        .where(IngestionStage.document_id == document_id, IngestionStage.stage.in_(later))
        .values(
            status=StageStatus.PENDING, done_units=0, total_units=None,
            started_at=None, finished_at=None, detail={},
        )
    )


def snapshot(session: Session, document_id: uuid.UUID) -> dict[str, Any]:
    rows = {
        row.stage: row
        for row in session.scalars(
            select(IngestionStage).where(IngestionStage.document_id == document_id)
        )
    }
    now = datetime.now(UTC)
    stages = []
    fractions = []
    for stage in STAGE_ORDER:
        row = rows.get(stage)
        if row is None:
            continue
        fraction = _fraction(row)
        if row.status != StageStatus.SKIPPED:
            fractions.append(fraction)
        stages.append(
            {
                "stage": stage,
                "status": row.status,
                "done_units": row.done_units,
                "total_units": row.total_units,
                "percent": round(fraction * 100, 1) if fraction is not None else None,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "estimated_seconds_remaining": _eta_seconds(row, now),
                "detail": row.detail,
            }
        )
    known = [f for f in fractions if f is not None]
    overall = round(sum(known) / len(fractions) * 100, 1) if fractions else 0.0
    return {"stages": stages, "overall_percent": overall}


def _fraction(row: IngestionStage) -> float | None:
    if row.status in (StageStatus.COMPLETED, StageStatus.SKIPPED):
        return 1.0
    if row.status == StageStatus.RUNNING and row.total_units:
        return min(row.done_units / row.total_units, 1.0)
    if row.status == StageStatus.RUNNING:
        return None  # running without a measurable total: no made-up percentage
    return 0.0


def _eta_seconds(row: IngestionStage, now: datetime) -> int | None:
    if row.status != StageStatus.RUNNING or not row.total_units or not row.started_at:
        return None
    done = row.done_units
    if done < MIN_UNITS_FOR_ETA or done / row.total_units < MIN_FRACTION_FOR_ETA:
        return None
    window = (row.detail or {}).get("rate_window") or []
    if window:
        since, units = datetime.fromisoformat(window[0][0]), window[0][1]
        span = (now - since).total_seconds()
        if span >= MIN_RATE_WINDOW_SECONDS and done > units:
            return int((row.total_units - done) / ((done - units) / span))
    elapsed = (now - row.started_at).total_seconds()
    if elapsed <= 0:
        return None
    rate = done / elapsed
    return int((row.total_units - done) / rate)
