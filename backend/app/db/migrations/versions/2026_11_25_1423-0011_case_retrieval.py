"""case retrieval (B22): pgvector, the cases table, similar cases on recommendations

- the pgvector extension (database image: backend/docker/postgres/Dockerfile)
- cases: one dispatched intervention = a normalised numeric feature vector of the situation
  (from MCP observations only), the action, predicted and (once measured) actual benefit
- recommendations.similar_cases / confidence / confidence_note: what the officer's card shows

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-25 14:23:29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.db.vector import Vector

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "cases",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("route_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("driver_instruction_id", sa.Uuid(), nullable=False),
        sa.Column(
            "action_type",
            sa.Enum(
                "hold",
                "skip_stop",
                "short_turn",
                "do_nothing",
                name="action_type",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=False,
        ),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("features", Vector(9), nullable=False),
        sa.Column("feature_values", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("predicted_benefit_passenger_minutes", sa.Double(), nullable=False),
        sa.Column("actual_benefit_passenger_minutes", sa.Double(), nullable=True),
        sa.Column("bunching_resolved", sa.Boolean(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("event_kind", sa.String(length=16), server_default="actual", nullable=False),
        sa.ForeignKeyConstraint(
            ["driver_instruction_id"],
            ["driver_instructions.id"],
            name=op.f("fk_cases_driver_instruction_id_driver_instructions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["route_id"], ["routes.id"], name=op.f("fk_cases_route_id_routes")),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["workflow_runs.id"],
            name=op.f("fk_cases_run_id_workflow_runs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cases")),
        sa.UniqueConstraint("driver_instruction_id", name=op.f("uq_cases_driver_instruction_id")),
    )
    op.create_index("ix_cases_route_created", "cases", ["route_id", "created_at"], unique=False)
    op.add_column(
        "recommendations",
        sa.Column("similar_cases", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "recommendations",
        sa.Column("confidence", sa.String(length=16), server_default="normal", nullable=False),
    )
    op.add_column("recommendations", sa.Column("confidence_note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("recommendations", "confidence_note")
    op.drop_column("recommendations", "confidence")
    op.drop_column("recommendations", "similar_cases")
    op.drop_index("ix_cases_route_created", table_name="cases")
    op.drop_table("cases")
    # the vector extension stays: other databases objects may use it
