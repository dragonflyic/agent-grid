"""Add agent_events table and Oz enrichment columns to executions.

Revision ID: 006
Revises: 005
Create Date: 2026-02-28 10:00:00.000000+00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: Union[str, None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Agent events table — append-only log of agent chat/tool events
    op.create_table(
        "agent_events",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column(
            "execution_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("message_type", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("tool_name", sa.Text(), nullable=True),
        sa.Column("tool_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index("idx_agent_events_execution_id", "agent_events", ["execution_id"])
    op.create_index(
        "idx_agent_events_execution_created",
        "agent_events",
        ["execution_id", "created_at"],
    )

    # Oz enrichment columns on executions
    op.add_column("executions", sa.Column("session_link", sa.Text(), nullable=True))
    op.add_column("executions", sa.Column("cost_cents", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("executions", "cost_cents")
    op.drop_column("executions", "session_link")
    op.drop_index("idx_agent_events_execution_created", table_name="agent_events")
    op.drop_index("idx_agent_events_execution_id", table_name="agent_events")
    op.drop_table("agent_events")
