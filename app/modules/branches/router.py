import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.pagination import Page, PageParams, page_params
from app.core.responses import ApiResponse, ok
from app.modules.auth.dependencies import CurrentPrincipal, require
from app.modules.auth.permissions import Permission, Principal
from app.modules.branches.schema import BranchCreate, BranchRead, BranchUpdate
from app.modules.branches.service import BranchService

router = APIRouter(prefix="/branches", tags=["branches"])

BranchAdmin = Annotated[Principal, Depends(require(Permission.BRANCHES_MANAGE))]


def get_service(db: Annotated[Session, Depends(get_db)]) -> BranchService:
    return BranchService(db)


Service = Annotated[BranchService, Depends(get_service)]


@router.post("", response_model=ApiResponse[BranchRead], status_code=201)
def create_branch(body: BranchCreate, principal: BranchAdmin, service: Service):
    return ok(BranchRead.model_validate(service.create(principal, body)))


@router.get("", response_model=ApiResponse[Page[BranchRead]])
def list_branches(
    principal: CurrentPrincipal, service: Service, page: Annotated[PageParams, Depends(page_params)]
):
    items, total = service.list(principal, limit=page.limit, offset=page.offset)
    return ok(Page(items=[BranchRead.model_validate(b) for b in items], total=total, **page.model_dump()))


@router.get("/{branch_id}", response_model=ApiResponse[BranchRead])
def get_branch(branch_id: uuid.UUID, principal: CurrentPrincipal, service: Service):
    return ok(BranchRead.model_validate(service.get(principal, branch_id)))


@router.patch("/{branch_id}", response_model=ApiResponse[BranchRead])
def update_branch(branch_id: uuid.UUID, body: BranchUpdate, principal: BranchAdmin, service: Service):
    return ok(BranchRead.model_validate(service.update(principal, branch_id, body)))
