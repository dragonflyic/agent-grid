"""Add partial unique index on executions to prevent duplicate active claims.

Revision ID: 003
Revises: 002
Create Date: 2026-02-14 00:00:00.000000+00:00
"""

from typing import Sequence, Union

from alembic import op

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "idx_executions_active_issue",
        "executions",
        ["issue_id"],
        unique=True,
        postgresql_where="status IN ('pending', 'running')",
    )


def downgrade() -> None:
    op.drop_index("idx_executions_active_issue", table_name="executions")
