import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import AuthenticationError, ValidationError
from app.core.security import (
    create_access_token,
    generate_opaque_token,
    hash_password,
    hash_token,
    password_needs_rehash,
    verify_password,
)
from app.modules.audit.service import record_event
from app.modules.auth.dependencies import principal_from_user
from app.modules.auth.model import RefreshToken
from app.modules.auth.permissions import Principal
from app.modules.auth.schema import Me, TokenPair
from app.modules.organizations.model import Organization, OrganizationStatus
from app.modules.users.model import User

INVALID_CREDENTIALS = "Invalid email or password."


class AuthService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    def login(self, email: str, password: str) -> TokenPair:
        user = self.session.scalar(
            select(User).where(func.lower(User.email) == email.lower()).with_for_update()
        )
        now = datetime.now(UTC)

        if user is not None and user.locked_until and user.locked_until > now:
            verify_password(password, None)  # keep timing uniform
            raise AuthenticationError(
                "Account temporarily locked after repeated failed logins.", code="ACCOUNT_LOCKED"
            )

        if not verify_password(password, user.password_hash if user else None):
            if user is not None:
                self._register_failure(user, now)
            raise AuthenticationError(INVALID_CREDENTIALS, code="INVALID_CREDENTIALS")

        if not user.is_active:
            raise AuthenticationError(INVALID_CREDENTIALS, code="INVALID_CREDENTIALS")
        if user.organization_id is not None:
            organization = self.session.get(Organization, user.organization_id)
            if organization.status != OrganizationStatus.ACTIVE:
                raise AuthenticationError(
                    "Organization is not active.", code="ORGANIZATION_SUSPENDED"
                )

        if password_needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = now
        tokens, _ = self._issue_tokens(user, family_id=uuid.uuid4())
        record_event(
            self.session,
            "auth.login",
            actor=principal_from_user(user, self.session),
            resource_type="user",
            resource_id=user.id,
        )
        self.session.commit()
        return tokens

    def refresh(self, refresh_token: str) -> TokenPair:
        now = datetime.now(UTC)
        stored = self.session.scalar(
            select(RefreshToken)
            .where(RefreshToken.token_hash == hash_token(refresh_token))
            .with_for_update()
        )
        if stored is None:
            raise AuthenticationError("Invalid refresh token.", code="INVALID_TOKEN")
        if stored.revoked_at is not None:
            # A rotated token was presented again: assume theft and end the whole session family.
            self._revoke_family(stored.family_id, now)
            record_event(
                self.session,
                "auth.refresh_token_reuse",
                resource_type="user",
                resource_id=stored.user_id,
                organization_id=self._organization_of(stored.user_id),
            )
            self.session.commit()
            raise AuthenticationError("Invalid refresh token.", code="INVALID_TOKEN")
        if stored.expires_at <= now:
            raise AuthenticationError("Refresh token expired.", code="TOKEN_EXPIRED")

        user = self.session.get(User, stored.user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("Invalid refresh token.", code="INVALID_TOKEN")
        if user.organization_id is not None:
            organization = self.session.get(Organization, user.organization_id)
            if organization.status != OrganizationStatus.ACTIVE:
                raise AuthenticationError(
                    "Organization is not active.", code="ORGANIZATION_SUSPENDED"
                )

        tokens, new_token_id = self._issue_tokens(user, family_id=stored.family_id)
        stored.revoked_at = now
        stored.replaced_by_id = new_token_id
        self.session.commit()
        return tokens

    def logout(self, refresh_token: str) -> None:
        stored = self.session.scalar(
            select(RefreshToken).where(RefreshToken.token_hash == hash_token(refresh_token))
        )
        if stored is not None:
            self._revoke_family(stored.family_id, datetime.now(UTC))
            self.session.commit()

    def change_password(self, principal: Principal, current: str, new: str) -> None:
        user = self.session.get(User, principal.user_id, with_for_update=True)
        if not verify_password(current, user.password_hash):
            raise ValidationError("Current password is incorrect.", code="INVALID_CREDENTIALS")
        if current == new:
            raise ValidationError("New password must differ from the current password.")
        user.password_hash = hash_password(new)
        # Log out every session, including this one's other devices.
        user.token_version += 1
        self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        record_event(
            self.session, "auth.password_changed", actor=principal, resource_type="user",
            resource_id=user.id,
        )
        self.session.commit()

    def me(self, principal: Principal) -> Me:
        user = self.session.get(User, principal.user_id)
        organization = (
            self.session.get(Organization, user.organization_id) if user.organization_id else None
        )
        return Me(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=user.role,
            organization_id=user.organization_id,
            organization_name=organization.name if organization else None,
            branch_id=user.branch_id,
            department_id=user.department_id,
            permissions=sorted(principal.permissions),
        )

    def _issue_tokens(self, user: User, *, family_id: uuid.UUID) -> tuple[TokenPair, uuid.UUID]:
        access_token, expires_in = create_access_token(
            self.settings,
            user_id=user.id,
            token_version=user.token_version,
            claims={"role": user.role, "org": str(user.organization_id or "")},
        )
        refresh_token = generate_opaque_token()
        stored = RefreshToken(
            id=uuid.uuid4(),
            user_id=user.id,
            family_id=family_id,
            token_hash=hash_token(refresh_token),
            expires_at=datetime.now(UTC) + timedelta(days=self.settings.REFRESH_TOKEN_TTL_DAYS),
        )
        self.session.add(stored)
        pair = TokenPair(
            access_token=access_token, refresh_token=refresh_token, expires_in=expires_in
        )
        return pair, stored.id

    def _register_failure(self, user: User, now: datetime) -> None:
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= self.settings.LOGIN_MAX_FAILED_ATTEMPTS:
            user.locked_until = now + timedelta(minutes=self.settings.LOGIN_LOCKOUT_MINUTES)
            user.failed_login_attempts = 0
        record_event(
            self.session,
            "auth.login_failed",
            organization_id=user.organization_id,
            resource_type="user",
            resource_id=user.id,
            details={"locked": user.locked_until is not None and user.locked_until > now},
        )
        self.session.commit()

    def _revoke_family(self, family_id: uuid.UUID, now: datetime) -> None:
        self.session.execute(
            update(RefreshToken)
            .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=now)
        )

    def _organization_of(self, user_id: uuid.UUID) -> uuid.UUID | None:
        user = self.session.get(User, user_id)
        return user.organization_id if user else None
