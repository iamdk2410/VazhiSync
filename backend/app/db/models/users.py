import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, str_enum, utc_now_column, uuid_pk
from app.db.enums import Role


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # Emails are stored lower-cased by the API; the index enforces it case-insensitively anyway.
        Index("uq_users_email_lower", func.lower(text("email")), unique=True),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(str_enum(Role, "role"))
    display_name: Mapped[str] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    created_at: Mapped[datetime] = utc_now_column()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FavouriteStop(Base):
    __tablename__ = "favourite_stops"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    stop_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # GTFS stop_id
    alert_minutes: Mapped[int] = mapped_column(server_default=text("5"))
    created_at: Mapped[datetime] = utc_now_column()
