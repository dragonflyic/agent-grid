"""Add external_run_id column to executions for Oz run recovery.

Revision ID: 004
Revises: 003
Create Date: 2026-02-15 00:00:00.000000+00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("executions", sa.Column("external_run_id", sa.Text(), nullable=True))
    op.create_index(
        "idx_executions_external_run_id",
        "executions",
        ["external_run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_executions_external_run_id", table_name="executions")
    op.drop_column("executions", "external_run_id")
