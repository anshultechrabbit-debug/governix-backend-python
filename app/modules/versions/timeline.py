"""Effective-date timeline of a policy's versions.

Versions form a chain of half-open ranges [effective_from, effective_to). A new
version is inserted where its effective date falls (it may be historical), the
neighbours are re-linked, and nothing is overwritten. The database exclusion
constraint is the final guard against overlaps.
"""

import uuid
from datetime import date, timedelta

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, ValidationError
from app.modules.documents.model import DocumentSection
from app.modules.policies.model import AUTO_DATE_SOURCES, Policy, PolicyVersion, VersionStatus
from app.modules.versions.comparison import SectionView, compare_sections


def active_versions(session: Session, policy_id: uuid.UUID, *, lock: bool = False) -> list[PolicyVersion]:
    query = (
        select(PolicyVersion)
        .where(PolicyVersion.policy_id == policy_id, PolicyVersion.status == VersionStatus.ACTIVE)
        .order_by(PolicyVersion.effective_from)
    )
    if lock:
        query = query.with_for_update()
    return list(session.scalars(query))


def version_as_of(session: Session, policy_id: uuid.UUID, as_of: date) -> PolicyVersion | None:
    return session.scalar(
        select(PolicyVersion).where(
            PolicyVersion.policy_id == policy_id,
            PolicyVersion.status == VersionStatus.ACTIVE,
            effective_on(as_of),
        )
    )


def effective_on(as_of: date):
    """SQL predicate: the version was in force on `as_of`."""
    return and_(
        PolicyVersion.effective_from <= as_of,
        or_(PolicyVersion.effective_to.is_(None), PolicyVersion.effective_to > as_of),
    )


def timeline_state(version: PolicyVersion, today: date) -> str:
    if version.status != VersionStatus.ACTIVE:
        return version.status
    if version.effective_from > today:
        return "scheduled"
    if version.effective_to is not None and version.effective_to <= today:
        return "historical"
    return "current"


def insert_version(session: Session, policy: Policy, new: PolicyVersion) -> tuple[PolicyVersion | None, PolicyVersion | None]:
    """Place `new` in the timeline. Returns its (previous, next) neighbours."""
    # Lock the policy so concurrent confirmations serialise on the timeline.
    session.scalar(select(Policy.id).where(Policy.id == policy.id).with_for_update())
    versions = active_versions(session, policy.id, lock=True)
    start = new.effective_from
    _make_room(session, versions, start)
    previous = max((v for v in versions if v.effective_from < start), key=lambda v: v.effective_from, default=None)
    following = min((v for v in versions if v.effective_from > start), key=lambda v: v.effective_from, default=None)

    if new.effective_to is not None:
        if new.effective_to <= start:
            raise ValidationError("effective_to must be after effective_from.")
        if following and new.effective_to > following.effective_from:
            raise ConflictError(
                "The effective period overlaps the next version.",
                code="VERSION_CONFLICT",
                details={"type": "OVERLAPPING_PERIOD", "existing_version_id": str(following.id)},
            )
    else:
        new.effective_to = following.effective_from if following else None

    if previous and (previous.effective_to is None or previous.effective_to > start):
        previous.effective_to = start
    # Shorten the neighbour before inserting, so the exclusion constraint never sees an overlap.
    session.flush()
    new.supersedes_version_id = previous.id if previous else None
    new.superseded_by_version_id = following.id if following else None
    session.add(new)
    session.flush()
    if previous:
        previous.superseded_by_version_id = new.id
    if following:
        following.supersedes_version_id = new.id
    session.flush()
    return previous, following


def _make_room(session: Session, versions: list[PolicyVersion], day: date) -> None:
    """Free `day` for a new version by moving placeholder-dated versions one day earlier.

    A version registered without an effective date carries a placeholder (its
    upload date, or a day before its successor). When another version is added
    on the same day, the older placeholder yields: it moves back a day, and so
    does any placeholder it then lands on. A real date (entered, or stated in
    the document) never moves; colliding with one is a conflict for a person.
    """
    chain: list[PolicyVersion] = []
    current = day
    while (clash := next((v for v in versions if v.effective_from == current), None)) is not None:
        if clash.effective_date_source not in AUTO_DATE_SOURCES:
            raise ConflictError(
                f"Version {clash.version_label} is already effective from {current.isoformat()}.",
                code="VERSION_CONFLICT",
                details={"type": "EFFECTIVE_DATE_EXISTS", "existing_version_id": str(clash.id)},
            )
        chain.append(clash)
        current -= timedelta(days=1)
    # Oldest first: shorten the predecessor, then extend the moved version back,
    # so the exclusion constraint never sees two ranges overlap.
    for version in reversed(chain):
        new_start = version.effective_from - timedelta(days=1)
        predecessor = max((v for v in versions if v.effective_from < new_start), key=lambda v: v.effective_from, default=None)
        if predecessor is not None and (predecessor.effective_to is None or predecessor.effective_to > new_start):
            predecessor.effective_to = new_start
            session.flush()
        version.effective_from = new_start
        session.flush()


def withdraw_version(session: Session, version: PolicyVersion, reason: str) -> None:
    """Take a version out of the timeline (never deleted). Its predecessor fills the gap."""
    session.scalar(select(Policy.id).where(Policy.id == version.policy_id).with_for_update())
    previous = session.get(PolicyVersion, version.supersedes_version_id) if version.supersedes_version_id else None
    following = session.get(PolicyVersion, version.superseded_by_version_id) if version.superseded_by_version_id else None
    version.status = VersionStatus.WITHDRAWN
    version.withdrawn_reason = reason
    session.flush()
    if previous is not None and previous.status == VersionStatus.ACTIVE:
        previous.effective_to = following.effective_from if following else None
        previous.superseded_by_version_id = following.id if following else None
    if following is not None and following.status == VersionStatus.ACTIVE:
        following.supersedes_version_id = previous.id if previous else None
    session.flush()


def restore_version(session: Session, version: PolicyVersion) -> tuple[PolicyVersion | None, PolicyVersion | None]:
    """Put a withdrawn version back into the timeline at its effective date.

    The inverse of `withdraw_version`: its predecessor is shortened again and
    it runs until the next active version. A placeholder-dated version in the
    way yields a day (as for a new version); a real date is a conflict.
    Returns its (previous, next) neighbours.
    """
    policy = session.get(Policy, version.policy_id)
    session.scalar(select(Policy.id).where(Policy.id == policy.id).with_for_update())
    versions = active_versions(session, policy.id, lock=True)
    start = version.effective_from
    _make_room(session, versions, start)
    previous = max((v for v in versions if v.effective_from < start), key=lambda v: v.effective_from, default=None)
    following = min((v for v in versions if v.effective_from > start), key=lambda v: v.effective_from, default=None)

    if previous and (previous.effective_to is None or previous.effective_to > start):
        previous.effective_to = start
    version.effective_to = following.effective_from if following else None
    # Shorten the neighbour before re-activating, so the exclusion constraint never sees an overlap.
    session.flush()
    version.status = VersionStatus.ACTIVE
    version.withdrawn_reason = None
    version.supersedes_version_id = previous.id if previous else None
    version.superseded_by_version_id = following.id if following else None
    if previous:
        previous.superseded_by_version_id = version.id
    if following:
        following.supersedes_version_id = version.id
    session.flush()
    return previous, following


def plan_order(newest_first: list[PolicyVersion]) -> dict[uuid.UUID, date]:
    """Effective dates that put the versions in this order (newest first), changing as little as possible.

    A real date (stated in the document or entered by a person) never moves: an
    order that contradicts one is a conflict for a person to resolve. A
    placeholder date keeps its value when it already fits, else moves to the
    day before its successor. The new newest version takes the latest
    placeholder date, so the one dragged to the top is the one in force.
    """
    anchor = max((v.effective_from for v in newest_first if v.effective_date_source in AUTO_DATE_SOURCES), default=None)
    planned: dict[uuid.UUID, date] = {}
    following: PolicyVersion | None = None
    for version in newest_first:
        next_day = planned[following.id] if following else None
        if version.effective_date_source not in AUTO_DATE_SOURCES:
            if next_day is not None and version.effective_from >= next_day:
                stated = "stated in the document" if version.effective_date_source == "detected" else "entered"
                raise ConflictError(
                    f"Version {version.version_label} is effective from {version.effective_from.isoformat()} "
                    f"({stated}), so it cannot come before version {following.version_label}. "
                    "Change its effective date to reorder these versions.",
                    code="VERSION_ORDER_CONFLICT",
                    details={"version_id": str(version.id), "blocked_by_version_id": str(following.id)},
                )
            day = version.effective_from
        elif next_day is None:
            day = max(version.effective_from, anchor)
        else:
            day = version.effective_from if version.effective_from < next_day else next_day - timedelta(days=1)
        planned[version.id] = day
        following = version
    return planned


def in_force(planned: dict[uuid.UUID, date], newest_first: list[PolicyVersion], end: date | None, today: date) -> uuid.UUID | None:
    """Which version the planned dates put in force today."""
    chronological = list(reversed(newest_first))
    for index, version in enumerate(chronological):
        start = planned[version.id]
        stop = planned[chronological[index + 1].id] if index + 1 < len(chronological) else end
        if start <= today and (stop is None or today < stop):
            return version.id
    return None


def reorder_versions(session: Session, policy: Policy, newest_first: list[PolicyVersion]) -> None:
    """Rebuild the active timeline in the given order (see `plan_order`). Nothing is deleted."""
    session.scalar(select(Policy.id).where(Policy.id == policy.id).with_for_update())
    planned = plan_order(newest_first)
    chronological = list(reversed(newest_first))
    # The policy's own end (an expiry date on its latest version) carries over to the new latest.
    end = max(newest_first, key=lambda v: v.effective_from).effective_to
    # Step out of the no-overlap constraint while ranges move, then re-enter oldest first.
    for version in chronological:
        version.status = VersionStatus.WITHDRAWN
    session.flush()
    for index, version in enumerate(chronological):
        following = chronological[index + 1] if index + 1 < len(chronological) else None
        if planned[version.id] != version.effective_from:
            version.effective_from = planned[version.id]
            version.effective_date_source = "inferred"  # placed by the order a person chose
        version.effective_to = planned[following.id] if following else (end if end and end > version.effective_from else None)
        version.supersedes_version_id = chronological[index - 1].id if index else None
        version.superseded_by_version_id = following.id if following else None
        if version.version_label_auto:
            version.version_label = str(index + 1)
        version.status = VersionStatus.ACTIVE
        session.flush()
    refresh_change_summaries(session, *chronological)


def section_views(session: Session, document_id: uuid.UUID) -> list[SectionView]:
    rows = session.scalars(
        select(DocumentSection)
        .where(DocumentSection.document_id == document_id)
        .order_by(DocumentSection.order_index)
    )
    return [
        SectionView(s.number, s.title, s.content, s.page_start, s.page_end, s.content_hash, s.level)
        for s in rows
    ]


def compare_versions(session: Session, old: PolicyVersion, new: PolicyVersion, *, include_content: bool = True) -> dict:
    result = compare_sections(
        section_views(session, old.document_id),
        section_views(session, new.document_id),
        include_content=include_content,
    )
    result["from_version"] = {"id": str(old.id), "label": old.version_label, "effective_from": old.effective_from.isoformat()}
    result["to_version"] = {"id": str(new.id), "label": new.version_label, "effective_from": new.effective_from.isoformat()}
    return result


def refresh_change_summaries(session: Session, *versions: PolicyVersion | None) -> None:
    """Recompute each version's stored diff against its current predecessor."""
    for version in versions:
        if version is None:
            continue
        previous = session.get(PolicyVersion, version.supersedes_version_id) if version.supersedes_version_id else None
        version.change_summary = compare_versions(session, previous, version, include_content=False) if previous else None
        version.ai_change_summary = None  # stale once the deterministic diff changes
