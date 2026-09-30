import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.departments.model import Department


class DepartmentRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_in_org(self, organization_id: uuid.UUID, department_id: uuid.UUID) -> Department | None:
        return self.session.scalar(
            select(Department).where(
                Department.id == department_id, Department.organization_id == organization_id
            )
        )

    def list(
        self,
        organization_id: uuid.UUID,
        *,
        branch_id: uuid.UUID | None,
        limit: int,
        offset: int,
    ) -> tuple[list[Department], int]:
        query = select(Department).where(Department.organization_id == organization_id)
        if branch_id is not None:
            query = query.where(Department.branch_id == branch_id)
        total = self.session.scalar(select(func.count()).select_from(query.subquery()))
        items = self.session.scalars(
            query.order_by(Department.name).limit(limit).offset(offset)
        ).all()
        return list(items), total or 0
