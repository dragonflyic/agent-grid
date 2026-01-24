"""Shared Pydantic models for Agent Grid."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    """Return current UTC time as timezone-aware datetime."""
    return datetime.now(timezone.utc)


class ExecutionStatus(str, Enum):
    """Status of an agent execution."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class IssueStatus(str, Enum):
    """Status of an issue."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"


class EventType(str, Enum):
    """Types of events in the event bus."""

    AGENT_STARTED = "agent.started"
    AGENT_PROGRESS = "agent.progress"
    AGENT_COMPLETED = "agent.completed"
    AGENT_FAILED = "agent.failed"
    ISSUE_CREATED = "issue.created"
    ISSUE_UPDATED = "issue.updated"
    NUDGE_REQUESTED = "nudge.requested"


class AgentExecution(BaseModel):
    """Represents an agent execution."""

    id: UUID
    issue_id: str
    repo_url: str
    status: ExecutionStatus = ExecutionStatus.PENDING
    prompt: str | None = None
    result: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)


class Comment(BaseModel):
    """A comment on an issue."""

    id: str
    body: str
    created_at: datetime = Field(default_factory=utc_now)


class IssueInfo(BaseModel):
    """Information about an issue from the issue tracker."""

    id: str
    number: int
    title: str
    body: str | None = None
    status: IssueStatus = IssueStatus.OPEN
    labels: list[str] = Field(default_factory=list)
    repo_url: str
    html_url: str
    parent_id: str | None = None
    blocked_by: list[str] = Field(default_factory=list)
    comments: list[Comment] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class NudgeRequest(BaseModel):
    """A request from an agent to nudge another issue/agent."""

    id: UUID
    issue_id: str
    source_execution_id: UUID | None = None
    priority: int = 0
    reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    processed_at: datetime | None = None


class Event(BaseModel):
    """An event in the event bus."""

    type: EventType
    timestamp: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)


class LaunchAgentRequest(BaseModel):
    """Request to launch an agent."""

    issue_id: str
    repo_url: str
    prompt: str


class NudgeRequestCreate(BaseModel):
    """Request to create a nudge."""

    issue_id: str
    source_execution_id: UUID | None = None
    priority: int = 0
    reason: str | None = None
