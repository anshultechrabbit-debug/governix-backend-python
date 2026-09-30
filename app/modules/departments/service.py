import uuid

from sqlalchemy.orm import Session

from app.core.database import translate_unique_violation
from app.core.exceptions import NotFoundError, PermissionDeniedError
from app.modules.audit.service import record_event
from app.modules.auth.permissions import Principal, Role
from app.modules.auth.scope import tenant_id
from app.modules.branches.repository import BranchRepository
from app.modules.departments.model import Department
from app.modules.departments.repository import DepartmentRepository
from app.modules.departments.schema import DepartmentCreate, DepartmentUpdate


class DepartmentService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = DepartmentRepository(session)
        self.branches = BranchRepository(session)

    def _check_branch_access(self, principal: Principal, branch_id: uuid.UUID) -> None:
        if principal.role is not Role.ORG_ADMIN and branch_id != principal.branch_id:
            raise PermissionDeniedError("You can only manage departments in your own branch.")

    def create(self, principal: Principal, data: DepartmentCreate) -> Department:
        organization_id = tenant_id(principal)
        if self.branches.get_in_org(organization_id, data.branch_id) is None:
            raise NotFoundError("Branch not found.")
        self._check_branch_access(principal, data.branch_id)
        department = Department(
            organization_id=organization_id, branch_id=data.branch_id, name=data.name, code=data.code
        )
        with translate_unique_violation(
            self.session, "A department with this code already exists in the branch."
        ):
            self.session.add(department)
            self.session.flush()
            record_event(
                self.session, "department.created", actor=principal, resource_type="department",
                resource_id=department.id,
                details={"name": data.name, "code": data.code, "branch_id": str(data.branch_id)},
            )
            self.session.commit()
        return department

    def get(self, principal: Principal, department_id: uuid.UUID) -> Department:
        department = self.repository.get_in_org(tenant_id(principal), department_id)
        if department is None or (
            principal.role is not Role.ORG_ADMIN and department.branch_id != principal.branch_id
        ):
            raise NotFoundError("Department not found.")
        return department

    def list(
        self, principal: Principal, *, branch_id: uuid.UUID | None, limit: int, offset: int
    ) -> tuple[list[Department], int]:
        if principal.role is not Role.ORG_ADMIN:
            branch_id = principal.branch_id
        return self.repository.list(
            tenant_id(principal), branch_id=branch_id, limit=limit, offset=offset
        )

    def update(
        self, principal: Principal, department_id: uuid.UUID, data: DepartmentUpdate
    ) -> Department:
        department = self.get(principal, department_id)
        self._check_branch_access(principal, department.branch_id)
        changes = data.model_dump(exclude_unset=True, exclude_none=True)
        for field, value in changes.items():
            setattr(department, field, value)
        record_event(
            self.session, "department.updated", actor=principal, resource_type="department",
            resource_id=department.id, details={"changes": changes},
        )
        self.session.commit()
        return department
