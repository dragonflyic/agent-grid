"""Add issue_state table and checkpoint column to executions.

Revision ID: 002
Revises: 001
Create Date: 2026-02-08 00:00:00.000000+00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add checkpoint and mode columns to executions
    op.add_column("executions", sa.Column("checkpoint", sa.JSON(), nullable=True))
    op.add_column("executions", sa.Column("mode", sa.Text(), nullable=True, server_default="implement"))
    op.add_column("executions", sa.Column("pr_number", sa.Integer(), nullable=True))
    op.add_column("executions", sa.Column("branch", sa.Text(), nullable=True))

    # Create issue_state table
    op.create_table(
        "issue_state",
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("classification", sa.Text(), nullable=True),
        sa.Column("parent_issue", sa.Integer(), nullable=True),
        sa.Column("sub_issues", sa.ARRAY(sa.Integer()), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("issue_number", "repo"),
    )
    op.create_index("idx_issue_state_classification", "issue_state", ["classification"])
    op.create_index("idx_issue_state_repo", "issue_state", ["repo"])

    # Create cron_state table
    op.create_table(
        "cron_state",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("cron_state")
    op.drop_index("idx_issue_state_repo", table_name="issue_state")
    op.drop_index("idx_issue_state_classification", table_name="issue_state")
    op.drop_table("issue_state")
    op.drop_column("executions", "branch")
    op.drop_column("executions", "pr_number")
    op.drop_column("executions", "mode")
    op.drop_column("executions", "checkpoint")
