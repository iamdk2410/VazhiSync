"""Public auth routes: register (always a passenger), login, and the current user."""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import LoginRequest, RegisterRequest, TokenResponse, UserOut
from app.auth.deps import ANY_ROLE, require_role
from app.auth.rate_limit import rate_limited
from app.auth.security import create_access_token
from app.auth.service import EmailTakenError, authenticate, create_user
from app.db.enums import Role
from app.db.models import User
from app.db.session import get_session

log = structlog.get_logger()
router = APIRouter(prefix="/auth", tags=["auth"])

Session = Annotated[AsyncSession, Depends(get_session)]


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=UserOut,
    dependencies=[Depends(rate_limited("register"))],
)
async def register(body: RegisterRequest, session: Session) -> User:
    # Rule 10: self-registration can only ever create a passenger. The body has no role
    # field, and unknown fields are rejected (422).
    try:
        user = await create_user(
            session,
            email=body.email,
            password=body.password,
            role=Role.PASSENGER,
            display_name=body.display_name,
        )
    except EmailTakenError:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Email already registered") from None
    log.info("user_registered", user_id=str(user.id), role=user.role)
    return user


@router.post("/login", response_model=TokenResponse, dependencies=[Depends(rate_limited("login"))])
async def login(body: LoginRequest, session: Session) -> TokenResponse:
    user = await authenticate(session, email=body.email, password=body.password)
    if user is None:
        log.info("login_failed")
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token, expires_in = create_access_token(user.id, user.role)
    log.info("login_succeeded", user_id=str(user.id), role=user.role)
    return TokenResponse(access_token=token, expires_in=expires_in, role=user.role)


@router.get("/me", response_model=UserOut)
async def me(user: Annotated[User, Depends(require_role(*ANY_ROLE))]) -> User:
    return user
