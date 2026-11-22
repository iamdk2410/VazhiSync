"""User creation and credential checks, shared by the API and the seed script."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import burn_password_check, hash_password, verify_password
from app.db.enums import Role
from app.db.models import User


class EmailTakenError(Exception):
    pass


def normalize_email(email: str) -> str:
    return email.strip().lower()


async def create_user(
    session: AsyncSession, *, email: str, password: str, role: Role, display_name: str
) -> User:
    user = User(
        email=normalize_email(email),
        password_hash=hash_password(password),
        role=role,
        display_name=display_name.strip(),
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError as exc:  # unique index on lower(email)
        await session.rollback()
        raise EmailTakenError(email) from exc
    await session.refresh(user)
    return user


async def authenticate(session: AsyncSession, *, email: str, password: str) -> User | None:
    user = await session.scalar(
        select(User).where(func.lower(User.email) == normalize_email(email))
    )
    if user is None:
        burn_password_check(password)
        return None
    valid, new_hash = verify_password(password, user.password_hash)
    if not valid or not user.is_active:
        return None
    if new_hash is not None:
        user.password_hash = new_hash
    user.last_login_at = datetime.now(UTC)
    await session.commit()
    return user
