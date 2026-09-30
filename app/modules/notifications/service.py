"""Who hears about what (spec §40-44).

    NEW_POLICY               a global policy is published -> every Branch Manager;
                             Users hear about a policy only once it is assigned to them
    POLICY_VERSION_CHANGED   a new version is now in force -> its assigned Users and the
                             managers who can see it, with the deterministic change summary
    POLICY_UPDATED           a version was scheduled, withdrawn, restored or moved
    POLICY_ASSIGNED/REMOVED  -> the User concerned
    POLICY_EXPIRING/EXPIRED  daily sweep -> assigned Users and managers
    TICKET_UPDATED, COMPLAINT_RESPONSE, SUPPORT_RESPONSE  -> see tickets.service

Notifications are written in the caller's transaction, so an event that rolls
back never notifies. A dedupe key makes repeated delivery (job retries, the
daily sweep) a no-op.
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.modules.assignments.model import PolicyAssignment
from app.modules.auth.permissions import Principal, Role
from app.modules.notifications.model import Notification, NotificationType
from app.modules.policies.model import Policy, PolicyStatus, PolicyVersion, VersionStatus
from app.modules.users.model import User

EXPIRY_NOTICE_DAYS = 30
# How long after expiry an "expired" notice is still worth sending (e.g. after downtime).
EXPIRED_NOTICE_WINDOW_DAYS = 7


def notify(
    session: Session,
    user_ids: Iterable[uuid.UUID],
    type_: NotificationType,
    title: str,
    *,
    organization_id: uuid.UUID | None,
    body: str | None = None,
    link: str | None = None,
    data: dict[str, Any] | None = None,
    dedupe_key: str | None = None,
    exclude: uuid.UUID | None = None,
) -> int:
    recipients = [u for u in dict.fromkeys(user_ids) if u is not None and u != exclude]
    if not recipients:
        return 0
    rows = [
        {
            "id": uuid.uuid4(), "organization_id": organization_id, "user_id": user_id, "type": type_,
            "title": title[:300], "body": body, "link": link, "data": data or {}, "dedupe_key": dedupe_key,
        }
        for user_id in recipients
    ]
    statement = insert(Notification).values(rows).on_conflict_do_nothing(
        index_elements=["user_id", "dedupe_key"], index_where=text("dedupe_key IS NOT NULL"),
    ).returning(Notification.id)
    return len(session.execute(statement).all())


# --- audiences -----------------------------------------------------------------


def assigned_users(session: Session, policy_id: uuid.UUID) -> set[uuid.UUID]:
    return set(session.scalars(
        select(PolicyAssignment.user_id)
        .join(User, User.id == PolicyAssignment.user_id)
        .where(PolicyAssignment.policy_id == policy_id, PolicyAssignment.removed_at.is_(None), User.is_active)
    ))


def managers_of(session: Session, policy: Policy) -> set[uuid.UUID]:
    """Branch Managers who can see the policy: all of them for a global policy, else its branch's."""
    query = select(User.id).where(
        User.organization_id == policy.organization_id, User.role == Role.BRANCH_MANAGER, User.is_active,
    )
    if policy.branch_id is not None:
        query = query.where(User.branch_id == policy.branch_id)
    return set(session.scalars(query))


def audience(session: Session, policy: Policy) -> set[uuid.UUID]:
    return assigned_users(session, policy.id) | managers_of(session, policy)


# --- policy events ------------------------------------------------------------------


def _change_lines(version: PolicyVersion, limit: int = 5) -> list[str]:
    lines = (version.change_summary or {}).get("summary_lines") or []
    return lines[:limit]


def version_published(session: Session, policy: Policy, version: PolicyVersion, *, today: date, actor_id: uuid.UUID | None) -> None:
    """A version's document became searchable: tell the people it concerns."""
    from app.modules.versions.timeline import timeline_state

    link = f"/policies/{policy.id}"
    data = {"policy_id": str(policy.id), "version_id": str(version.id), "version_label": version.version_label}
    active = session.scalar(
        select(func.count()).where(PolicyVersion.policy_id == policy.id, PolicyVersion.status == VersionStatus.ACTIVE)
    )
    state = timeline_state(version, today)
    if active == 1:
        # A new policy. Global ones go to every manager; Users hear on assignment.
        scope = "global" if policy.branch_id is None else "branch"
        notify(
            session, managers_of(session, policy), NotificationType.NEW_POLICY,
            f"New {scope} policy available: {policy.name}",
            body=f"{policy.name} v{version.version_label} has been published.",
            organization_id=policy.organization_id, link=link, data=data,
            dedupe_key=f"published:{version.id}", exclude=actor_id,
        )
        return
    if state == "current":
        changes = _change_lines(version)
        body = f"{policy.name} has been updated to version {version.version_label}."
        if changes:
            body += " Important changes: " + "; ".join(changes)
        notify(
            session, audience(session, policy), NotificationType.POLICY_VERSION_CHANGED,
            f"Policy updated: {policy.name} v{version.version_label}", body=body,
            organization_id=policy.organization_id, link=f"{link}?tab=changes", data=data,
            dedupe_key=f"published:{version.id}", exclude=actor_id,
        )
    elif state == "scheduled":
        notify(
            session, audience(session, policy), NotificationType.POLICY_UPDATED,
            f"Upcoming change: {policy.name} v{version.version_label}",
            body=f"Version {version.version_label} takes effect on {version.effective_from.isoformat()}.",
            organization_id=policy.organization_id, link=f"{link}?tab=versions", data=data,
            dedupe_key=f"published:{version.id}", exclude=actor_id,
        )


def latest_changed(session: Session, policy: Policy, version: PolicyVersion | None, *, reason: str, actor_id: uuid.UUID | None) -> None:
    """The version in force changed by a person's action (reorder, withdraw, restore, move)."""
    label = f" (now v{version.version_label})" if version else ""
    notify(
        session, audience(session, policy), NotificationType.POLICY_VERSION_CHANGED,
        f"Policy updated: {policy.name}{label}", body=reason,
        organization_id=policy.organization_id, link=f"/policies/{policy.id}?tab=versions",
        data={"policy_id": str(policy.id), "version_id": str(version.id) if version else None},
        exclude=actor_id,
    )


def assignment_changed(session: Session, policy: Policy, user_id: uuid.UUID, *, assigned: bool, actor_id: uuid.UUID | None) -> None:
    if assigned:
        notify(
            session, [user_id], NotificationType.POLICY_ASSIGNED, f"New policy assigned: {policy.name}",
            body=f"{policy.name} has been assigned to you.", organization_id=policy.organization_id,
            link=f"/policies/{policy.id}", data={"policy_id": str(policy.id)}, exclude=actor_id,
        )
    else:
        notify(
            session, [user_id], NotificationType.POLICY_REMOVED, f"Policy removed: {policy.name}",
            body=f"{policy.name} is no longer assigned to you.", organization_id=policy.organization_id,
            data={"policy_id": str(policy.id)}, exclude=actor_id,
        )


def expiry_sweep(session: Session, today: date) -> int:
    """Notify about versions in force that end soon, or ended recently with nothing after them."""
    sent = 0
    rows = session.execute(
        select(PolicyVersion, Policy)
        .join(Policy, Policy.id == PolicyVersion.policy_id)
        .where(
            Policy.status == PolicyStatus.ACTIVE,
            PolicyVersion.status == VersionStatus.ACTIVE,
            PolicyVersion.effective_to.is_not(None),
            PolicyVersion.effective_to > today - timedelta(days=EXPIRED_NOTICE_WINDOW_DAYS),
            PolicyVersion.effective_to <= today + timedelta(days=EXPIRY_NOTICE_DAYS),
        )
    ).all()
    for version, policy in rows:
        # Only when nothing takes over: then the policy itself ends.
        successor = session.scalar(
            select(PolicyVersion.id).where(
                PolicyVersion.policy_id == policy.id, PolicyVersion.status == VersionStatus.ACTIVE,
                PolicyVersion.effective_from >= version.effective_to,
            ).limit(1)
        )
        if successor is not None:
            continue
        ends = version.effective_to
        expired = ends <= today
        sent += notify(
            session, audience(session, policy),
            NotificationType.POLICY_EXPIRED if expired else NotificationType.POLICY_EXPIRING,
            f"Policy {'expired' if expired else 'expiring'}: {policy.name}",
            body=(f"{policy.name} v{version.version_label} "
                  + (f"expired on {ends.isoformat()}." if expired else f"expires on {ends.isoformat()}.")),
            organization_id=policy.organization_id, link=f"/policies/{policy.id}",
            data={"policy_id": str(policy.id), "version_id": str(version.id), "ends": ends.isoformat()},
            dedupe_key=f"{'expired' if expired else 'expiring'}:{version.id}:{ends.isoformat()}",
        )
    return sent


# --- inbox --------------------------------------------------------------------------


def inbox(session: Session, principal: Principal, *, unread_only: bool, limit: int, offset: int) -> tuple[list[Notification], int, int]:
    query = select(Notification).where(Notification.user_id == principal.user_id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    unread = session.scalar(
        select(func.count()).where(Notification.user_id == principal.user_id, Notification.read_at.is_(None))
    ) or 0
    items = session.scalars(query.order_by(Notification.created_at.desc()).limit(limit).offset(offset)).all()
    return list(items), total, unread


def mark_read(session: Session, principal: Principal, notification_ids: list[uuid.UUID] | None) -> int:
    """Mark the given notifications (or all) as read. Only ever the caller's own."""
    statement = update(Notification).where(
        Notification.user_id == principal.user_id, Notification.read_at.is_(None)
    ).values(read_at=datetime.now(UTC))
    if notification_ids is not None:
        statement = statement.where(Notification.id.in_(notification_ids))
    count = session.execute(statement).rowcount or 0
    session.commit()
    return count
