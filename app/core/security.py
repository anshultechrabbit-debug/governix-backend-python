import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import Settings
from app.core.exceptions import AuthenticationError

_password_hasher = PasswordHasher()
# Verified against when the user does not exist, so login timing does not
# reveal which emails are registered.
_DUMMY_HASH = _password_hasher.hash(secrets.token_urlsafe(16))

MIN_PASSWORD_LENGTH = 12


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _password_hasher.verify(password_hash or _DUMMY_HASH, password) and bool(
            password_hash
        )
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _password_hasher.check_needs_rehash(password_hash)


def create_access_token(
    settings: Settings, *, user_id: uuid.UUID, token_version: int, claims: dict[str, Any]
) -> tuple[str, int]:
    now = datetime.now(UTC)
    ttl = timedelta(minutes=settings.ACCESS_TOKEN_TTL_MINUTES)
    payload = {
        **claims,
        "sub": str(user_id),
        "tv": token_version,
        "type": "access",
        "iat": now,
        "exp": now + ttl,
        "jti": uuid.uuid4().hex,
    }
    token = jwt.encode(
        payload, settings.JWT_SECRET.get_secret_value(), algorithm=settings.JWT_ALGORITHM
    )
    return token, int(ttl.total_seconds())


def decode_access_token(settings: Settings, token: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(
            token,
            settings.JWT_SECRET.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "iat", "sub", "type"]},
        )
    except jwt.ExpiredSignatureError:
        raise AuthenticationError("Access token expired.", code="TOKEN_EXPIRED") from None
    except jwt.PyJWTError:
        raise AuthenticationError("Invalid access token.", code="INVALID_TOKEN") from None
    if payload.get("type") != "access":
        raise AuthenticationError("Invalid access token.", code="INVALID_TOKEN")
    return payload


def generate_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    """Refresh tokens are stored hashed; a database leak does not yield usable tokens."""
    return hashlib.sha256(token.encode()).hexdigest()
