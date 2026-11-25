"""workflow_runs.last_active_at, and runs paused before 0007 marked as waiting for an officer

- last_active_at: when a worker last claimed the run (start or resume). A run still 'running'
  long after that can't be alive (every segment is capped by the run time limit), so a new
  event for its bus pair may close it instead of being blocked forever.
- Runs left 'awaiting_approval' / 'escalated' before migration 0007 have no waiting_for; they
  are waiting for an officer, so say so (they can then be superseded like any other).

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-25 07:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workflow_runs",
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.execute("UPDATE workflow_runs SET last_active_at = started_at")
    op.execute(
        "UPDATE workflow_runs SET waiting_for = 'officer' "
        "WHERE waiting_for IS NULL AND finished_at IS NULL "
        "AND status IN ('awaiting_approval', 'escalated')"
    )


def downgrade() -> None:
    op.drop_column("workflow_runs", "last_active_at")
