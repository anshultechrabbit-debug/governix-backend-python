from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_cache
from app.core.responses import ApiResponse, ok
from app.infrastructure.cache.base import Cache
from app.modules.auth.dependencies import require
from app.modules.auth.permissions import Permission, Principal
from app.modules.search.schema import SearchRequest, SearchResponse
from app.modules.search.service import SearchService
from app.workers.runtime import get_runtime

router = APIRouter(prefix="/search", tags=["search"])

Searcher = Annotated[Principal, Depends(require(Permission.SEARCH))]


def get_service(
    request: Request, db: Annotated[Session, Depends(get_db)], cache: Annotated[Cache, Depends(get_cache)]
) -> SearchService:
    return SearchService(db, request.app.state.session_factory, get_runtime().embedder, cache)


@router.post("", response_model=ApiResponse[SearchResponse])
def search(body: SearchRequest, principal: Searcher, service: Annotated[SearchService, Depends(get_service)]):
    """Hybrid search over authorised, effective content (passages carry full provenance)."""
    return ok(service.search(principal, body))
