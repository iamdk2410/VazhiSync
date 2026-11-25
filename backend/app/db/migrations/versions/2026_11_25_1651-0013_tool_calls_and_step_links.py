"""tool calls and step links (B18): one row per harness tool call; LLM calls know their step

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-25 16:51:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=True),
        sa.Column("agent", sa.String(length=64), nullable=False),
        sa.Column("tool", sa.String(length=64), nullable=False),
        sa.Column("server", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tool_calls")),
    )
    op.create_index("ix_tool_calls_created", "tool_calls", ["created_at"], unique=False)
    op.create_index(
        "ix_tool_calls_run_created", "tool_calls", ["run_id", "created_at"], unique=False
    )
    op.add_column("llm_usage", sa.Column("step_index", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("llm_usage", "step_index")
    op.drop_index("ix_tool_calls_run_created", table_name="tool_calls")
    op.drop_index("ix_tool_calls_created", table_name="tool_calls")
    op.drop_table("tool_calls")
