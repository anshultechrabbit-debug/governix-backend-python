import uuid

from app.core.exceptions import PermissionDeniedError
from app.modules.auth.permissions import Principal


def tenant_id(principal: Principal) -> uuid.UUID:
    """Organisation of a tenant user; platform admins have no tenant data access."""
    if principal.organization_id is None:
        raise PermissionDeniedError("This action requires an organization account.")
    return principal.organization_id
