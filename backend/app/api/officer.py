from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.schemas import AlertsResponse, BunchingEventOut
from app.auth.deps import require_role
from app.db.enums import BunchingEventStatus, Role
from app.db.models import BunchingEvent, User
from app.db.session import get_session

router = APIRouter(prefix="/officer", tags=["officer"])

Officer = Annotated[User, Depends(require_role(Role.OFFICER))]


@router.get("/alerts", response_model=AlertsResponse)
async def list_alerts(
    _: Officer, session: Annotated[AsyncSession, Depends(get_session)]
) -> AlertsResponse:
    """Open bunching events, newest first (the officer's alert queue)."""
    rows = await session.scalars(
        select(BunchingEvent)
        .where(BunchingEvent.status.in_([BunchingEventStatus.OPEN, BunchingEventStatus.HANDLING]))
        .order_by(BunchingEvent.detected_at.desc())
        .limit(100)
    )
    return AlertsResponse(alerts=[BunchingEventOut.model_validate(r) for r in rows])
