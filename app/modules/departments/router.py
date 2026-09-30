import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.pagination import Page, PageParams, page_params
from app.core.responses import ApiResponse, ok
from app.modules.auth.dependencies import CurrentPrincipal, require
from app.modules.auth.permissions import Permission, Principal
from app.modules.departments.schema import DepartmentCreate, DepartmentRead, DepartmentUpdate
from app.modules.departments.service import DepartmentService

router = APIRouter(prefix="/departments", tags=["departments"])

DepartmentAdmin = Annotated[Principal, Depends(require(Permission.DEPARTMENTS_MANAGE))]


def get_service(db: Annotated[Session, Depends(get_db)]) -> DepartmentService:
    return DepartmentService(db)


Service = Annotated[DepartmentService, Depends(get_service)]


@router.post("", response_model=ApiResponse[DepartmentRead], status_code=201)
def create_department(body: DepartmentCreate, principal: DepartmentAdmin, service: Service):
    return ok(DepartmentRead.model_validate(service.create(principal, body)))


@router.get("", response_model=ApiResponse[Page[DepartmentRead]])
def list_departments(
    principal: CurrentPrincipal,
    service: Service,
    page: Annotated[PageParams, Depends(page_params)],
    branch_id: uuid.UUID | None = None,
):
    items, total = service.list(principal, branch_id=branch_id, limit=page.limit, offset=page.offset)
    return ok(Page(items=[DepartmentRead.model_validate(d) for d in items], total=total, **page.model_dump()))


@router.get("/{department_id}", response_model=ApiResponse[DepartmentRead])
def get_department(department_id: uuid.UUID, principal: CurrentPrincipal, service: Service):
    return ok(DepartmentRead.model_validate(service.get(principal, department_id)))


@router.patch("/{department_id}", response_model=ApiResponse[DepartmentRead])
def update_department(
    department_id: uuid.UUID, body: DepartmentUpdate, principal: DepartmentAdmin, service: Service
):
    return ok(DepartmentRead.model_validate(service.update(principal, department_id, body)))
