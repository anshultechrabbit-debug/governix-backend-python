import uuid

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.modules.organizations.model import Organization


class OrganizationRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, organization_id: uuid.UUID) -> Organization | None:
        return self.session.get(Organization, organization_id)

    def list(self, *, limit: int, offset: int) -> tuple[list[Organization], int]:
        total = self.session.scalar(select(func.count()).select_from(Organization))
        items = self.session.scalars(
            select(Organization).order_by(Organization.name).limit(limit).offset(offset)
        ).all()
        return list(items), total or 0

    def add(self, organization: Organization) -> Organization:
        self.session.add(organization)
        self.session.flush()
        return organization


def bump_knowledge_version(session: Session, organization_id: uuid.UUID) -> None:
    """Invalidate every cached answer/retrieval for the organisation (part of cache keys)."""
    session.execute(
        update(Organization)
        .where(Organization.id == organization_id)
        .values(knowledge_version=Organization.knowledge_version + 1)
    )
