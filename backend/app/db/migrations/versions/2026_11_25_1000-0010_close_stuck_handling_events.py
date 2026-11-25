"""close bunching events left in 'handling' by runs that have finished (data fix, with B21)

Until now a "do nothing" run left its event in 'handling' for the world runner to close once the
headway recovered, but the runner only closed 'open' events; superseded and abandoned runs left
theirs too. Such events looked busy forever (the early-warning sweep skipped their pairs). The
code now closes them; this dismisses the ones already stuck: 'handling' with no unfinished run.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-25 10:00:00

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE bunching_events e
        SET status = 'dismissed', resolved_at = now()
        WHERE e.status = 'handling'
          AND NOT EXISTS (
            SELECT 1 FROM workflow_runs r
            WHERE r.bunching_event_id = e.id AND r.finished_at IS NULL
          )
        """
    )


def downgrade() -> None:
    pass  # a data fix: the old, stuck state isn't worth restoring
