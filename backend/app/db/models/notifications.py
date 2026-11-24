import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, str_enum, utc_now_column, uuid_pk
from app.db.enums import NotificationKind


class Notification(Base):
    """A message to one user (user_id set) or a public service alert (user_id NULL)."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_created", "user_id", text("created_at DESC")),
        Index("ix_notifications_stop", "stop_id"),
        CheckConstraint("user_id IS NOT NULL OR route_id IS NOT NULL", name="has_audience"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    route_id: Mapped[str | None] = mapped_column(ForeignKey("routes.id"))
    stop_id: Mapped[str | None] = mapped_column(ForeignKey("stops.id"))
    driver_instruction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("driver_instructions.id", ondelete="SET NULL")
    )
    kind: Mapped[NotificationKind] = mapped_column(str_enum(NotificationKind, "kind"))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = utc_now_column()
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
