"""approval, dispatch and outcomes (B12)

- recommendations.expires_at: a recommendation is re-planned once it is too old to trust
- driver_instructions: 'cant_comply' status with the driver's reason (and optional note)
- intervention_outcomes: predicted vs actual benefit, in passenger-minutes
- workflow_runs.waiting_for: which interrupt a paused run waits on (officer / driver / outcome)

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-24 19:22:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_STATUSES = "'sent', 'acknowledged', 'done', 'expired', 'cancelled'"
NEW_STATUSES = OLD_STATUSES + ", 'cant_comply'"


def _status_check(values: str) -> None:
    op.drop_constraint(op.f("ck_driver_instructions_status"), "driver_instructions", type_="check")
    op.create_check_constraint(
        op.f("ck_driver_instructions_status"),
        "driver_instructions",
        f"status IN ({values})",
    )


def upgrade() -> None:
    op.add_column("workflow_runs", sa.Column("waiting_for", sa.String(length=16), nullable=True))
    op.add_column(
        "recommendations", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "driver_instructions", sa.Column("cancel_reason", sa.String(length=32), nullable=True)
    )
    op.add_column("driver_instructions", sa.Column("cancel_note", sa.Text(), nullable=True))
    _status_check(NEW_STATUSES)
    op.create_check_constraint(
        op.f("ck_driver_instructions_cant_comply_has_reason"),
        "driver_instructions",
        "status <> 'cant_comply' OR cancel_reason IS NOT NULL",
    )
    op.add_column(
        "intervention_outcomes",
        sa.Column("predicted_benefit_passenger_minutes", sa.Double(), nullable=True),
    )
    op.add_column(
        "intervention_outcomes",
        sa.Column("actual_benefit_passenger_minutes", sa.Double(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("intervention_outcomes", "actual_benefit_passenger_minutes")
    op.drop_column("intervention_outcomes", "predicted_benefit_passenger_minutes")
    op.drop_constraint(
        op.f("ck_driver_instructions_cant_comply_has_reason"), "driver_instructions", type_="check"
    )
    op.execute("UPDATE driver_instructions SET status = 'cancelled' WHERE status = 'cant_comply'")
    _status_check(OLD_STATUSES)
    op.drop_column("driver_instructions", "cancel_note")
    op.drop_column("driver_instructions", "cancel_reason")
    op.drop_column("recommendations", "expires_at")
    op.drop_column("workflow_runs", "waiting_for")
