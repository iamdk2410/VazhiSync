from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import AssignmentOut, AssignmentResponse
from app.auth.deps import require_role
from app.db.enums import Role
from app.db.models import DriverAssignment, User, Vehicle
from app.db.session import get_session

router = APIRouter(prefix="/driver", tags=["driver"])

Driver = Annotated[User, Depends(require_role(Role.DRIVER))]


@router.get("/assignment", response_model=AssignmentResponse)
async def current_assignment(
    driver: Driver, session: Annotated[AsyncSession, Depends(get_session)]
) -> AssignmentResponse:
    """The bus this driver is currently assigned to (only their own)."""
    row = (
        await session.execute(
            select(DriverAssignment, Vehicle)
            .join(Vehicle, Vehicle.id == DriverAssignment.vehicle_id)
            .where(DriverAssignment.driver_id == driver.id, DriverAssignment.ends_at.is_(None))
        )
    ).first()
    if row is None:
        return AssignmentResponse(assignment=None)
    assignment, vehicle = row
    return AssignmentResponse(
        assignment=AssignmentOut(
            vehicle_id=vehicle.id,
            fleet_number=vehicle.fleet_number,
            route_id=vehicle.route_id,
            starts_at=assignment.starts_at,
        )
    )
