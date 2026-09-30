from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.core.dependencies import get_app_settings
from app.core.responses import ApiResponse, ok
from app.modules.auth.dependencies import CurrentPrincipal
from app.modules.auth.schema import (
    ChangePasswordRequest,
    LoginRequest,
    Me,
    RefreshRequest,
    TokenPair,
)
from app.modules.auth.service import AuthService

router = APIRouter(prefix="/auth", tags=["auth"])


def get_auth_service(
    db: Annotated[Session, Depends(get_db)],
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> AuthService:
    return AuthService(db, settings)


Service = Annotated[AuthService, Depends(get_auth_service)]


@router.post("/login", response_model=ApiResponse[TokenPair])
def login(body: LoginRequest, service: Service):
    return ok(service.login(body.email, body.password))


@router.post("/refresh", response_model=ApiResponse[TokenPair])
def refresh(body: RefreshRequest, service: Service):
    return ok(service.refresh(body.refresh_token))


@router.post("/logout", response_model=ApiResponse[dict])
def logout(body: RefreshRequest, service: Service):
    service.logout(body.refresh_token)
    return ok({})


@router.get("/me", response_model=ApiResponse[Me])
def me(principal: CurrentPrincipal, service: Service):
    return ok(service.me(principal))


@router.post("/change-password", response_model=ApiResponse[dict])
def change_password(body: ChangePasswordRequest, principal: CurrentPrincipal, service: Service):
    service.change_password(principal, body.current_password, body.new_password)
    return ok({})
