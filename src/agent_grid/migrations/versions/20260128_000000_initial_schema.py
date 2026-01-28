"""Initial schema for Agent Grid.

Revision ID: 001
Revises:
Create Date: 2026-01-28 00:00:00.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def table_exists(table_name: str) -> bool:
    """Check if a table exists in the database."""
    bind = op.get_bind()
    inspector = inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    # Skip if tables already exist (migrating from hand-rolled schema)
    if table_exists("executions"):
        print("Tables already exist, skipping initial schema creation")
        return

    # Create executions table
    op.create_table(
        "executions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("issue_id", sa.Text(), nullable=False),
        sa.Column("repo_url", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_executions_issue_id", "executions", ["issue_id"])
    op.create_index("idx_executions_status", "executions", ["status"])
    op.create_index("idx_executions_created_at", "executions", ["created_at"])

    # Create nudge_queue table
    op.create_table(
        "nudge_queue",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("issue_id", sa.Text(), nullable=False),
        sa.Column("source_execution_id", sa.UUID(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_nudge_queue_processed",
        "nudge_queue",
        ["processed_at"],
        postgresql_where=sa.text("processed_at IS NULL"),
    )

    # Create budget_usage table
    op.create_table(
        "budget_usage",
        sa.Column(
            "id",
            sa.UUID(),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("execution_id", sa.UUID(), nullable=False),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_seconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("idx_budget_usage_recorded_at", "budget_usage", ["recorded_at"])

    # Create alembic_version table marker for existing databases
    # This helps with the transition from hand-rolled schema


def downgrade() -> None:
    op.drop_index("idx_budget_usage_recorded_at", table_name="budget_usage")
    op.drop_table("budget_usage")

    op.drop_index("idx_nudge_queue_processed", table_name="nudge_queue")
    op.drop_table("nudge_queue")

    op.drop_index("idx_executions_created_at", table_name="executions")
    op.drop_index("idx_executions_status", table_name="executions")
    op.drop_index("idx_executions_issue_id", table_name="executions")
    op.drop_table("executions")
