from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response, status
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import FavouritesResponse, FavouriteStopIn, FavouriteStopOut, StopId
from app.auth.deps import require_role
from app.db.enums import Role
from app.db.models import FavouriteStop, User
from app.db.session import get_session

router = APIRouter(prefix="/passenger", tags=["passenger"])

Passenger = Annotated[User, Depends(require_role(Role.PASSENGER))]
Session = Annotated[AsyncSession, Depends(get_session)]
StopIdPath = Annotated[StopId, Path()]


@router.get("/favourites", response_model=FavouritesResponse)
async def list_favourites(user: Passenger, session: Session) -> FavouritesResponse:
    rows = await session.scalars(
        select(FavouriteStop)
        .where(FavouriteStop.user_id == user.id)
        .order_by(FavouriteStop.created_at)
    )
    return FavouritesResponse(favourites=[FavouriteStopOut.model_validate(r) for r in rows])


@router.put("/favourites/{stop_id}", response_model=FavouriteStopOut)
async def save_favourite(
    stop_id: StopIdPath, body: FavouriteStopIn, user: Passenger, session: Session
) -> FavouriteStop:
    # Stop ids are checked against the imported route once B2 adds the stops table.
    stmt = (
        insert(FavouriteStop)
        .values(user_id=user.id, stop_id=stop_id, alert_minutes=body.alert_minutes)
        .on_conflict_do_update(
            index_elements=[FavouriteStop.user_id, FavouriteStop.stop_id],
            set_={"alert_minutes": body.alert_minutes},
        )
        .returning(FavouriteStop)
    )
    favourite = (await session.scalars(stmt)).one()
    await session.commit()
    return favourite


@router.delete("/favourites/{stop_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_favourite(stop_id: StopIdPath, user: Passenger, session: Session) -> Response:
    await session.execute(
        delete(FavouriteStop).where(
            FavouriteStop.user_id == user.id, FavouriteStop.stop_id == stop_id
        )
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
