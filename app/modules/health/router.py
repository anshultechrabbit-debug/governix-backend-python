import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.database import get_db
from app.core.dependencies import get_app_settings
from app.core.exceptions import ServiceUnavailableError
from app.core.responses import ApiResponse, ok

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


class HealthStatus(BaseModel):
    status: str
    service: str
    version: str


class DatabaseStatus(BaseModel):
    status: str
    database: str


@router.get("/health", response_model=ApiResponse[HealthStatus])
def health(settings: Annotated[Settings, Depends(get_app_settings)]):
    return ok(HealthStatus(status="OK", service=settings.APP_NAME, version=settings.APP_VERSION))


@router.get("/db-health", response_model=ApiResponse[DatabaseStatus])
def db_health(db: Annotated[Session, Depends(get_db)]):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        logger.exception("Database health check failed")
        raise ServiceUnavailableError(
            "Database is unavailable.", code="DATABASE_UNAVAILABLE"
        ) from None
    return ok(DatabaseStatus(status="OK", database="connected"))


@router.get("/metrics", include_in_schema=False)
def metrics(request: Request):
    """Prometheus-compatible, dependency-free process metrics."""
    return Response(request.app.state.metrics.render(), media_type="text/plain; version=0.0.4")
