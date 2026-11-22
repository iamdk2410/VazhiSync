"""Create officer and driver accounts (the only way these roles are created).

    uv run python -m app.db.seed --role officer --email officer@example.com --name "Officer One"
    uv run python -m app.db.seed --role driver --email d@example.com --name "Driver One" \
        --vehicle TN01AN1234

The password comes from --password, the SEED_PASSWORD environment variable, or a prompt.
"""

import argparse
import asyncio
import getpass
import os
import sys

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import select

from app.auth.security import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH
from app.auth.service import EmailTakenError, create_user
from app.db.enums import Role
from app.db.models import DriverAssignment, Vehicle
from app.db.session import get_engine, get_sessionmaker

SEEDABLE_ROLES = (Role.OFFICER, Role.DRIVER)
_email_adapter: TypeAdapter[str] = TypeAdapter(EmailStr)


class SeedError(Exception):
    pass


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.db.seed", description=__doc__)
    parser.add_argument("--role", required=True, choices=[r.value for r in SEEDABLE_ROLES])
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", help="display name (defaults to the email's local part)")
    parser.add_argument("--password", help="prefer SEED_PASSWORD or the prompt over this flag")
    parser.add_argument("--vehicle", help="driver only: fleet number of the bus to assign")
    args = parser.parse_args(argv)
    if args.vehicle and args.role != Role.DRIVER:
        parser.error("--vehicle is only valid with --role driver")
    return args


def resolve_password(flag_value: str | None) -> str:
    password = flag_value or os.environ.get("SEED_PASSWORD") or getpass.getpass("Password: ")
    if not PASSWORD_MIN_LENGTH <= len(password) <= PASSWORD_MAX_LENGTH:
        raise SeedError(
            f"password must be {PASSWORD_MIN_LENGTH}-{PASSWORD_MAX_LENGTH} characters long"
        )
    return password


async def seed(
    *, role: Role, email: str, password: str, name: str | None, vehicle: str | None
) -> str:
    if role not in SEEDABLE_ROLES:
        raise SeedError("only officer and driver accounts are seeded; passengers self-register")
    try:  # same email rules as POST /auth/register
        email = _email_adapter.validate_python(email)
    except ValidationError as exc:
        raise SeedError(f"invalid email: {exc.errors()[0]['msg']}") from None
    async with get_sessionmaker()() as session:
        try:
            user = await create_user(
                session,
                email=email,
                password=password,
                role=role,
                display_name=name or email.split("@")[0],
            )
        except EmailTakenError:
            raise SeedError(f"a user with email {email} already exists") from None
        message = f"created {role.value} {user.email} ({user.id})"
        if vehicle:
            bus = await session.scalar(select(Vehicle).where(Vehicle.fleet_number == vehicle))
            if bus is None:
                raise SeedError(f"user created, but vehicle {vehicle} does not exist (yet)")
            session.add(DriverAssignment(driver_id=user.id, vehicle_id=bus.id))
            await session.commit()
            message += f", assigned to {vehicle}"
    return message


async def _main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        message = await seed(
            role=Role(args.role),
            email=args.email,
            password=resolve_password(args.password),
            name=args.name,
            vehicle=args.vehicle,
        )
    except SeedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        await get_engine().dispose()
    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
