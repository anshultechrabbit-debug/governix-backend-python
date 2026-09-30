import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.pagination import Page, PageParams, page_params
from app.core.responses import ApiResponse, ok
from app.modules.auth.dependencies import CurrentPrincipal, require
from app.modules.auth.permissions import Permission, Principal
from app.modules.users.schema import PasswordReset, UserCreate, UserRead, UserUpdate
from app.modules.users.service import UserService

router = APIRouter(prefix="/users", tags=["users"])


UserAdmin = Annotated[Principal, Depends(require(Permission.USERS_MANAGE))]


def get_service(db: Annotated[Session, Depends(get_db)]) -> UserService:
    return UserService(db)


Service = Annotated[UserService, Depends(get_service)]


@router.post("", response_model=ApiResponse[UserRead], status_code=201)
def create_user(body: UserCreate, principal: UserAdmin, service: Service):
    return ok(UserRead.model_validate(service.create(principal, body)))


@router.get("", response_model=ApiResponse[Page[UserRead]])
def list_users(
    principal: UserAdmin,
    service: Service,
    page: Annotated[PageParams, Depends(page_params)],
    search: Annotated[str | None, Query(max_length=100)] = None,
):
    items, total = service.list(principal, search=search, limit=page.limit, offset=page.offset)
    return ok(Page(items=[UserRead.model_validate(u) for u in items], total=total, **page.model_dump()))


@router.get("/{user_id}", response_model=ApiResponse[UserRead])
def get_user(user_id: uuid.UUID, principal: CurrentPrincipal, service: Service):
    return ok(UserRead.model_validate(service.get(principal, user_id)))


@router.patch("/{user_id}", response_model=ApiResponse[UserRead])
def update_user(user_id: uuid.UUID, body: UserUpdate, principal: UserAdmin, service: Service):
    return ok(UserRead.model_validate(service.update(principal, user_id, body)))


@router.post("/{user_id}/reset-password", response_model=ApiResponse[dict])
def reset_password(user_id: uuid.UUID, body: PasswordReset, principal: UserAdmin, service: Service):
    service.reset_password(principal, user_id, body.new_password)
    return ok({})
