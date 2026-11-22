"""Declarative base. Models (B3) subclass `Base`; Alembic autogenerates from `Base.metadata`."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
