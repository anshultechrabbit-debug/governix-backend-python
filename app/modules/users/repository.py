import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.users.model import User


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, user_id: uuid.UUID) -> User | None:
        return self.session.get(User, user_id)

    def email_exists(self, email: str) -> bool:
        return (
            self.session.scalar(select(User.id).where(func.lower(User.email) == email.lower()))
            is not None
        )

    def list(
        self,
        *,
        organization_id: uuid.UUID | None,
        branch_id: uuid.UUID | None,
        roles: frozenset[str] | None,
        search: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[User], int]:
        query = select(User)
        if organization_id is not None:
            query = query.where(User.organization_id == organization_id)
        if branch_id is not None:
            query = query.where(User.branch_id == branch_id)
        if roles is not None:
            query = query.where(User.role.in_(roles))
        if search:
            pattern = f"%{search.lower()}%"
            query = query.where(
                func.lower(User.email).like(pattern) | func.lower(User.full_name).like(pattern)
            )
        total = self.session.scalar(select(func.count()).select_from(query.subquery()))
        items = self.session.scalars(query.order_by(User.email).limit(limit).offset(offset)).all()
        return list(items), total or 0
