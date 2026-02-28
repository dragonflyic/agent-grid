"""SQLAlchemy ORM models for the coordinator database.

These models are internal to the database layer. The public API uses
Pydantic models (AgentExecution, NudgeRequest) — conversion happens
in database.py.
"""

import uuid
from datetime import datetime

from sqlalchemy import ARRAY, DateTime, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    pass


class ExecutionModel(Base):
    __tablename__ = "executions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    issue_id: Mapped[str] = mapped_column(Text, nullable=False)
    repo_url: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[str | None] = mapped_column(Text, nullable=True, server_default="implement")
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    branch: Mapped[str | None] = mapped_column(Text, nullable=True)
    checkpoint: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    external_run_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    session_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    cost_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    __table_args__ = (
        Index("idx_executions_issue_id", "issue_id"),
        Index("idx_executions_status", "status"),
        Index("idx_executions_created_at", "created_at"),
        Index("idx_executions_external_run_id", "external_run_id"),
        Index(
            "idx_executions_active_issue",
            "issue_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )


class NudgeModel(Base):
    __tablename__ = "nudge_queue"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    issue_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "idx_nudge_queue_processed",
            "processed_at",
            postgresql_where=text("processed_at IS NULL"),
        ),
    )


class BudgetUsageModel(Base):
    __tablename__ = "budget_usage"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    __table_args__ = (Index("idx_budget_usage_recorded_at", "recorded_at"),)


class IssueStateModel(Base):
    __tablename__ = "issue_state"

    issue_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo: Mapped[str] = mapped_column(Text, primary_key=True)
    classification: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_issue: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sub_issues: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    metadata_: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    __table_args__ = (
        Index("idx_issue_state_classification", "classification"),
        Index("idx_issue_state_repo", "repo"),
    )


class CronStateModel(Base):
    __tablename__ = "cron_state"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))


class PipelineEventModel(Base):
    """Audit trail for pipeline decisions — append-only."""

    __tablename__ = "pipeline_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    issue_number: Mapped[int] = mapped_column(Integer, nullable=False)
    repo: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=text("NOW()"))

    __table_args__ = (
        Index("idx_pipeline_events_issue", "issue_number", "repo"),
        Index("idx_pipeline_events_type", "event_type"),
        Index("idx_pipeline_events_repo_created", "repo", "created_at"),
    )


class AgentEventModel(Base):
    """Append-only log of agent chat/tool events during execution."""

    __tablename__ = "agent_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    message_type: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    tool_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("NOW()")
    )

    __table_args__ = (
        Index("idx_agent_events_execution_id", "execution_id"),
        Index("idx_agent_events_execution_created", "execution_id", "created_at"),
    )
