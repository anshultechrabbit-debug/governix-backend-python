import uuid

from sqlalchemy.orm import Session

from app.core.database import translate_unique_violation
from app.core.exceptions import NotFoundError
from app.modules.audit.service import record_event
from app.modules.auth.permissions import Principal, Role
from app.modules.auth.scope import tenant_id
from app.modules.branches.model import Branch
from app.modules.branches.repository import BranchRepository
from app.modules.branches.schema import BranchCreate, BranchUpdate


class BranchService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.repository = BranchRepository(session)

    def _visible_branch_filter(self, principal: Principal) -> uuid.UUID | None:
        # Org admins see every branch; everyone else only their own.
        return None if principal.role is Role.ORG_ADMIN else principal.branch_id

    def create(self, principal: Principal, data: BranchCreate) -> Branch:
        organization_id = tenant_id(principal)
        branch = Branch(organization_id=organization_id, name=data.name, code=data.code)
        with translate_unique_violation(self.session, "A branch with this code already exists."):
            self.session.add(branch)
            self.session.flush()
            record_event(
                self.session, "branch.created", actor=principal, resource_type="branch",
                resource_id=branch.id, details={"name": data.name, "code": data.code},
            )
            self.session.commit()
        return branch

    def get(self, principal: Principal, branch_id: uuid.UUID) -> Branch:
        branch = self.repository.get_in_org(tenant_id(principal), branch_id)
        only = self._visible_branch_filter(principal)
        if branch is None or (only is not None and branch.id != only):
            raise NotFoundError("Branch not found.")
        return branch

    def list(self, principal: Principal, *, limit: int, offset: int) -> tuple[list[Branch], int]:
        return self.repository.list(
            tenant_id(principal),
            only_branch_id=self._visible_branch_filter(principal),
            limit=limit,
            offset=offset,
        )

    def update(self, principal: Principal, branch_id: uuid.UUID, data: BranchUpdate) -> Branch:
        branch = self.get(principal, branch_id)
        changes = data.model_dump(exclude_unset=True, exclude_none=True)
        for field, value in changes.items():
            setattr(branch, field, value)
        record_event(
            self.session, "branch.updated", actor=principal, resource_type="branch",
            resource_id=branch.id, details={"changes": changes},
        )
        self.session.commit()
        return branch
