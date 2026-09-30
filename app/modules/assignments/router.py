import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.responses import ApiResponse, ok
from app.modules.assignments.service import AssignableUser, AssignmentRead, AssignmentService
from app.modules.auth.dependencies import require
from app.modules.auth.permissions import Permission, Principal

router = APIRouter(tags=["policy assignments"])

Assigner = Annotated[Principal, Depends(require(Permission.POLICIES_ASSIGN))]


def get_service(db: Annotated[Session, Depends(get_db)]) -> AssignmentService:
    return AssignmentService(db)


Service = Annotated[AssignmentService, Depends(get_service)]


class AssignUsers(BaseModel):
    user_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


class AssignPolicies(BaseModel):
    policy_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)


@router.get("/policies/{policy_id}/assignments", response_model=ApiResponse[list[AssignmentRead]])
def policy_assignments(policy_id: uuid.UUID, principal: Assigner, service: Service, include_removed: bool = False):
    """Users this policy is assigned to (within the caller's branch); with history on request."""
    return ok(service.for_policy(principal, policy_id, include_removed=include_removed))


@router.post("/policies/{policy_id}/assignments", response_model=ApiResponse[list[AssignmentRead]])
def assign_users(policy_id: uuid.UUID, body: AssignUsers, principal: Assigner, service: Service):
    return ok(service.assign(principal, policy_id, body.user_ids))


@router.delete("/policies/{policy_id}/assignments/{user_id}", response_model=ApiResponse[dict])
def unassign_user(policy_id: uuid.UUID, user_id: uuid.UUID, principal: Assigner, service: Service):
    service.unassign(principal, policy_id, user_id)
    return ok({"removed": True})


@router.get("/policies/{policy_id}/assignable-users", response_model=ApiResponse[list[AssignableUser]])
def assignable_users(
    policy_id: uuid.UUID, principal: Assigner, service: Service,
    search: Annotated[str | None, Query(max_length=200)] = None,
):
    return ok(service.assignable_users(principal, policy_id, search))


@router.get("/users/{user_id}/assignments", response_model=ApiResponse[list[AssignmentRead]])
def user_assignments(user_id: uuid.UUID, principal: Assigner, service: Service, include_removed: bool = False):
    return ok(service.for_user(principal, user_id, include_removed=include_removed))


@router.post("/users/{user_id}/assignments", response_model=ApiResponse[list[AssignmentRead]])
def assign_policies(user_id: uuid.UUID, body: AssignPolicies, principal: Assigner, service: Service):
    return ok(service.assign_to_user(principal, user_id, body.policy_ids))
