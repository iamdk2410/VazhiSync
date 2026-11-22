"""Password hashing (argon2 via pwdlib) and JWT access tokens (PyJWT)."""

import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache

import jwt
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from pydantic import BaseModel, ValidationError

from app.config import get_settings
from app.db.enums import Role

JWT_ALGORITHM = "HS256"
PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 128

_hasher = PasswordHash((Argon2Hasher(),))


class InvalidTokenError(Exception):
    """Token missing, malformed, expired, tampered with, or with an invalid payload."""


class TokenPayload(BaseModel):
    sub: uuid.UUID
    role: Role
    iat: int
    exp: int


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> tuple[bool, str | None]:
    """Returns (valid, new_hash). new_hash is set when the stored hash should be upgraded."""
    return _hasher.verify_and_update(password, password_hash)


@lru_cache
def _dummy_hash() -> str:
    return _hasher.hash("timing-equaliser-not-a-real-password")


def burn_password_check(password: str) -> None:
    """Spend the same time as a real check, so unknown emails can't be found by timing."""
    _hasher.verify(password, _dummy_hash())


def create_access_token(user_id: uuid.UUID, role: Role) -> tuple[str, int]:
    settings = get_settings()
    now = datetime.now(UTC)
    expires_in = settings.jwt_expire_minutes * 60
    claims = {
        "sub": str(user_id),
        "role": role.value,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
    }
    token = jwt.encode(claims, settings.jwt_secret.get_secret_value(), algorithm=JWT_ALGORITHM)
    return token, expires_in


def decode_access_token(token: str) -> TokenPayload:
    try:
        claims = jwt.decode(
            token,
            get_settings().jwt_secret.get_secret_value(),
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "role", "iat", "exp"]},
        )
        return TokenPayload.model_validate(claims)
    except (jwt.PyJWTError, ValidationError) as exc:
        raise InvalidTokenError(str(exc)) from exc
