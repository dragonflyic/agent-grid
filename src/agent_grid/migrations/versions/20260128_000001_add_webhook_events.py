"""Add webhook_events table for deduplication queue.

Revision ID: 002
Revises: 001
Create Date: 2026-01-28 00:00:01.000000+00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create webhook_events table for deduplication
    op.create_table(
        "webhook_events",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("delivery_id", sa.Text(), nullable=False),  # X-GitHub-Delivery header
        sa.Column("event_type", sa.Text(), nullable=False),  # issues, issue_comment, etc.
        sa.Column("action", sa.Text(), nullable=True),  # opened, labeled, created, etc.
        sa.Column("repo", sa.Text(), nullable=True),
        sa.Column("issue_id", sa.Text(), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),  # JSON payload for processing
        sa.Column("processed", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("coalesced_into", sa.UUID(), nullable=True),  # Reference to primary event if deduplicated
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # Index for checking duplicate delivery IDs (idempotency)
    op.create_index(
        "idx_webhook_events_delivery_id",
        "webhook_events",
        ["delivery_id"],
        unique=True,
    )

    # Index for finding unprocessed events
    op.create_index(
        "idx_webhook_events_unprocessed",
        "webhook_events",
        ["processed", "received_at"],
        postgresql_where=sa.text("processed = false"),
    )

    # Index for finding events by issue for coalescing
    op.create_index(
        "idx_webhook_events_issue",
        "webhook_events",
        ["repo", "issue_id", "received_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_webhook_events_issue", table_name="webhook_events")
    op.drop_index("idx_webhook_events_unprocessed", table_name="webhook_events")
    op.drop_index("idx_webhook_events_delivery_id", table_name="webhook_events")
    op.drop_table("webhook_events")
