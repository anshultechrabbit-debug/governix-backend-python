import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.pagination import Page, PageParams, page_params
from app.core.responses import ApiResponse, ok
from app.modules.auth.dependencies import CurrentPrincipal, require
from app.modules.auth.permissions import Permission, Principal
from app.modules.categories import contents
from app.modules.categories.schema import (
    CategoryCreate,
    CategoryDocument,
    CategoryRead,
    CategorySummary,
    CategoryUpdate,
    PendingUpload,
)
from app.modules.categories.service import CategoryService
from app.modules.policies.service import today

router = APIRouter(prefix="/categories", tags=["categories"])

CategoryAdmin = Annotated[Principal, Depends(require(Permission.CATEGORIES_MANAGE))]
PolicyReader = Annotated[Principal, Depends(require(Permission.POLICIES_READ))]
DocumentReader = Annotated[Principal, Depends(require(Permission.DOCUMENTS_READ))]


def get_service(db: Annotated[Session, Depends(get_db)]) -> CategoryService:
    return CategoryService(db)


Service = Annotated[CategoryService, Depends(get_service)]


@router.get("", response_model=ApiResponse[list[CategoryRead]])
def list_categories(principal: CurrentPrincipal, service: Service, include_inactive: bool = False):
    return ok([CategoryRead.model_validate(c) for c in service.list(principal, include_inactive=include_inactive)])


@router.get("/overview", response_model=ApiResponse[list[CategorySummary]])
def categories_overview(principal: PolicyReader, service: Service, include_inactive: bool = True):
    """Every category with its policy count and last activity (over what the caller can see)."""
    return ok(service.overview(principal, include_inactive=include_inactive))


@router.post("", response_model=ApiResponse[CategoryRead], status_code=201)
def create_category(body: CategoryCreate, principal: CategoryAdmin, service: Service):
    return ok(CategoryRead.model_validate(service.create(principal, body)))


@router.get("/{category_id}", response_model=ApiResponse[CategorySummary])
def get_category(category_id: uuid.UUID, principal: PolicyReader, service: Service):
    return ok(service.summary(principal, category_id))


@router.patch("/{category_id}", response_model=ApiResponse[CategoryRead])
def update_category(category_id: uuid.UUID, body: CategoryUpdate, principal: CategoryAdmin, service: Service):
    return ok(CategoryRead.model_validate(service.update(principal, category_id, body)))


@router.get("/{category_id}/documents", response_model=ApiResponse[Page[CategoryDocument]])
def category_documents(
    category_id: uuid.UUID,
    principal: PolicyReader,
    service: Service,
    page: Annotated[PageParams, Depends(page_params)],
    search: Annotated[str | None, Query(max_length=200)] = None,
    status: Annotated[Literal["active", "archived"] | None, Query()] = None,
    version_state: Annotated[Literal[contents.VERSION_STATES] | None, Query()] = None,  # type: ignore[valid-type]
    sort: Annotated[Literal[contents.SORTS], Query()] = "custom",  # type: ignore[valid-type]
):
    """Policies in the category with every version and the file behind it.

    sort=custom is the arranged order (drag and drop); a version_state filter
    also narrows each policy's versions to the matching ones.
    """
    category = service.get(principal, category_id)
    items, total = contents.documents(
        service.session, principal, category, as_of=today(), search=search, status=status,
        version_state=version_state, sort=sort, limit=page.limit, offset=page.offset,
    )
    return ok(Page(items=items, total=total, **page.model_dump()))


@router.get("/{category_id}/pending", response_model=ApiResponse[list[PendingUpload]])
def category_pending_uploads(category_id: uuid.UUID, principal: DocumentReader, service: Service):
    """Files uploaded into the category that are still processing or waiting for review."""
    return ok(contents.pending_uploads(service.session, principal, service.get(principal, category_id)))
