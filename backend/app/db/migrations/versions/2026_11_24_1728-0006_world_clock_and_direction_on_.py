"""world clock and direction on observations; one instruction per approval (B7)

The what-if model starts from the world's clock and each bus's trip direction, as an AVL
feed reports them; staleness keeps using the wall-clock recorded_at.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-24 17:28:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(op.f("ix_driver_instructions_approval_id"), table_name="driver_instructions")
    op.create_unique_constraint(
        op.f("uq_driver_instructions_approval_id"), "driver_instructions", ["approval_id"]
    )
    op.add_column("stop_queues", sa.Column("sim_time_s", sa.Double(), nullable=True))
    op.add_column("stop_queues", sa.Column("direction_id", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_stop_queues_direction_0_or_1"), "stop_queues", "direction_id IN (0, 1)"
    )
    op.add_column("vehicle_positions", sa.Column("sim_time_s", sa.Double(), nullable=True))
    op.add_column("vehicle_positions", sa.Column("direction_id", sa.Integer(), nullable=True))
    op.create_check_constraint(
        op.f("ck_vehicle_positions_direction_0_or_1"),
        "vehicle_positions",
        "direction_id IN (0, 1)",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("uq_driver_instructions_approval_id"), "driver_instructions", type_="unique"
    )
    op.create_index(
        op.f("ix_driver_instructions_approval_id"), "driver_instructions", ["approval_id"]
    )
    op.drop_constraint(
        op.f("ck_vehicle_positions_direction_0_or_1"), "vehicle_positions", type_="check"
    )
    op.drop_column("vehicle_positions", "direction_id")
    op.drop_column("vehicle_positions", "sim_time_s")
    op.drop_constraint(op.f("ck_stop_queues_direction_0_or_1"), "stop_queues", type_="check")
    op.drop_column("stop_queues", "direction_id")
    op.drop_column("stop_queues", "sim_time_s")
