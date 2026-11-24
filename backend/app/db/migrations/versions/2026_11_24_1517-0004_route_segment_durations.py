"""route segment durations (B6 geometry follow-up)

Per stop-to-stop road stretch: OSRM free-flow duration and how its geometry was made.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-24 15:17:20.415242

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "route_direction_stops", sa.Column("segment_duration_s", sa.Double(), nullable=True)
    )
    op.add_column(
        "route_direction_stops",
        sa.Column("segment_geometry_source", sa.String(length=16), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_route_direction_stops_segment_duration_positive"),
        "route_direction_stops",
        "segment_duration_s IS NULL OR segment_duration_s > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_route_direction_stops_segment_duration_positive"),
        "route_direction_stops",
        type_="check",
    )
    op.drop_column("route_direction_stops", "segment_geometry_source")
    op.drop_column("route_direction_stops", "segment_duration_s")
