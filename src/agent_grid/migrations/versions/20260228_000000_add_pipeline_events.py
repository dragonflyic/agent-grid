"""Add pipeline_events table for audit trail.

Revision ID: 005
Revises: 004
Create Date: 2026-02-28 00:00:00.000000+00:00
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "pipeline_events",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("repo", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("detail", sa.dialects.postgresql.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index(
        "idx_pipeline_events_issue",
        "pipeline_events",
        ["issue_number", "repo"],
    )
    op.create_index(
        "idx_pipeline_events_type",
        "pipeline_events",
        ["event_type"],
    )
    op.create_index(
        "idx_pipeline_events_repo_created",
        "pipeline_events",
        ["repo", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_pipeline_events_repo_created", table_name="pipeline_events")
    op.drop_index("idx_pipeline_events_type", table_name="pipeline_events")
    op.drop_index("idx_pipeline_events_issue", table_name="pipeline_events")
    op.drop_table("pipeline_events")
