"""predicted bunching events (B21)

- bunching_events.kind: 'actual' (detected by the world runner from GPS) or 'predicted'
  (early warning: a do-nothing fast-forward from MCP observations bunches the pair soon)
- bunching_events.predicted_in_seconds: how far ahead the bunch was predicted

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-25 09:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "bunching_events",
        sa.Column("kind", sa.String(length=32), server_default="actual", nullable=False),
    )
    op.add_column("bunching_events", sa.Column("predicted_in_seconds", sa.Float(), nullable=True))
    op.create_check_constraint(
        op.f("ck_bunching_events_kind"), "bunching_events", "kind IN ('actual', 'predicted')"
    )
    op.create_check_constraint(
        op.f("ck_bunching_events_predicted_has_lead_time"),
        "bunching_events",
        "kind = 'actual' OR predicted_in_seconds IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_bunching_events_predicted_has_lead_time"), "bunching_events", type_="check"
    )
    op.drop_constraint(op.f("ck_bunching_events_kind"), "bunching_events", type_="check")
    op.drop_column("bunching_events", "predicted_in_seconds")
    op.drop_column("bunching_events", "kind")
