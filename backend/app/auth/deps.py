"""Authentication and role dependencies. Rule 10: roles are enforced here, on the server."""

from collections.abc import Awaitable, Callable
from typing import Annotated

import structlog
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import InvalidTokenError, decode_access_token
from app.db.enums import Role
from app.db.models import User
from app.db.session import get_session

log = structlog.get_logger()

_bearer = HTTPBearer(auto_error=False)

# Marker attribute so tests can prove every endpoint is guarded.
REQUIRED_ROLES_ATTR = "__required_roles__"

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    if credentials is None:
        raise _UNAUTHORIZED
    try:
        payload = decode_access_token(credentials.credentials)
    except InvalidTokenError:
        raise _UNAUTHORIZED from None
    user = await session.get(User, payload.sub)
    # The token's role must still match the database (a role change invalidates old tokens).
    if user is None or not user.is_active or user.role != payload.role:
        raise _UNAUTHORIZED
    return user


def require_role(*roles: Role) -> Callable[..., Awaitable[User]]:
    """Dependency that returns the current user, or 403 if their role is not allowed."""
    if not roles:
        raise ValueError("require_role needs at least one role")
    allowed = frozenset(roles)

    async def dependency(user: Annotated[User, Depends(get_current_user)]) -> User:
        if user.role not in allowed:
            log.info("forbidden", user_id=str(user.id), role=user.role, allowed=sorted(allowed))
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return user

    setattr(dependency, REQUIRED_ROLES_ATTR, allowed)
    return dependency


ANY_ROLE = tuple(Role)
